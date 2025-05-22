import re
import json5
from pathlib import Path
from docutils.parsers.rst import Directive
from docutils import nodes
import sphinx.application
import sphinx.writers.html5
from sphinx.writers.latex import LaTeXTranslator
import yowasp_wavedrom
import cairosvg
from lxml import etree

# Constants
MAX_WAVE_LENGTH = 22
FONT_SIZE = "15px"

class WaveDromDirective(Directive):
    required_arguments = 1
    has_content = True

    def run(self):
        self.assert_has_content()
        name, = self.arguments

        payload = re.sub(r"^..\s+wavedrom\s*::.+?\n", "\n", self.block_text)

        try:
            wavedrom_src = json5.loads(payload, allow_duplicate_keys=False)
        except ValueError as error:
            return [self.reporter.error(f"WaveJSON: {error}")]

        node = wavedrom_diagram(self.block_text, name=name, src=wavedrom_src,
                                loc=f'{self.state.document["source"]}:{self.lineno}')
        self.add_name(node)
        return [node]

class wavedrom_diagram(nodes.General, nodes.Inline, nodes.Element):
    pass

def adjust_svg(svg_filepath, font_size=FONT_SIZE):
    try:
        tree = etree.parse(svg_filepath)
        root = tree.getroot()

        namespaces = {'svg': 'http://www.w3.org/2000/svg'}

        for text_element in root.xpath('//svg:text', namespaces=namespaces):
            style = text_element.get("style", "")
            if "font-size" in style:
                style = re.sub(r"font-size:\s*\d+px", f"font-size: {font_size}", style)
            else:
                style += f"; font-size: {font_size};"
            if "fill" in style:
                style = re.sub(r"fill:\s*#[0-9a-fA-F]{3,6}", "fill: #00008B", style)
            else:
                style += "; fill: #00008B;"
            text_element.set("style", style)
            text_element.set("font-size", font_size)
            text_element.set("fill", "#00008B")

        for line_element in root.xpath('//svg:path | //svg:line', namespaces=namespaces):
            style = line_element.get("style", "")
            if "stroke" in style:
                style = re.sub(r"stroke:\s*#[0-9a-fA-F]{3,6}", "stroke: #000", style)
            else:
                style += "; stroke: #000;"
            line_element.set("style", style)
            line_element.set("stroke", "#000")
                # Post-process vertical padding for constant (flat) signals
                # Compress vertical height of flat signals
        y_offset = 0
        compressed_wave_height = 10  # pixels, compressed height for flat signals
        standard_wave_height = 40    # pixels, estimated normal height of wave row
        signal_groups = root.xpath('//svg:g[@class="wave"]', namespaces=namespaces)

        for group in signal_groups:
            paths = group.xpath('.//svg:path', namespaces=namespaces)
            is_flat = all(
                'v' not in (p.get('d') or '') and 'V' not in (p.get('d') or '')
                for p in paths
            )

            transform = group.get("transform", "")
            y_match = re.search(r'translate\(\s*0\s*,\s*([0-9.]+)\s*\)', transform)
            y_pos = float(y_match.group(1)) if y_match else 0

            new_y = y_pos - y_offset
            group.set("transform", f"translate(0,{new_y})")

            if is_flat:
                y_offset += (standard_wave_height - compressed_wave_height)
        tree.write(str(svg_filepath), encoding="utf-8", xml_declaration=True)
    except Exception as e:
        sphinx.application.logger.error(f"Error adjusting SVG: {e}")

def split_waveform_if_needed(wavedrom_src, max_length):
    signals = wavedrom_src.get("signal", [])
    max_wave_length = max((len(sig.get("wave", "")) for sig in signals), default=0)

    if max_wave_length <= max_length:
        return [wavedrom_src]

    num_segments = (max_wave_length + max_length - 1) // max_length
    segments = []

    for i in range(num_segments):
        segment = {"signal": [], "config": wavedrom_src.get("config", {}).copy()}
        for sig in signals:
            wave = sig.get("wave", "")
            wave_segment = wave[i * max_length : (i + 1) * max_length]
            wave_segment = wave_segment.ljust(max_length, '.')
            segment["signal"].append({ **sig, "wave": wave_segment })
        segments.append(segment)

    for seg_idx in range(1, len(segments)):
        for s_idx, sig in enumerate(segments[seg_idx]["signal"]):
            current_wave = sig.get("wave", "")
            if current_wave and current_wave[0] == '.':
                prev_wave = segments[seg_idx - 1]["signal"][s_idx].get("wave", "")
                last_state = next((ch for ch in reversed(prev_wave) if ch != '.'), None)
                if last_state is not None:
                    new_wave = last_state + current_wave[1:]
                    segments[seg_idx]["signal"][s_idx]["wave"] = new_wave

    return segments

def is_meaningful_segment(segment, previous_segment):
    for sig, prev_sig in zip(segment["signal"], previous_segment["signal"]):
        wave = sig.get("wave", "")
        prev_wave = prev_sig.get("wave", "")

        # Skip empty waves
        if not wave or set(wave) <= {"."}:
            continue

        # Significant if more than one unique state
        if len(set(wave) - {"."}) > 1:
            return True

        # If first state is different than last from previous
        first = next((c for c in wave if c != "."), None)
        last_prev = next((c for c in reversed(prev_wave) if c != "."), None)
        if first and last_prev and first != last_prev:
            return True

    return False

def html_visit_wavedrom_diagram(self: sphinx.writers.html5.HTML5Translator, node: wavedrom_diagram):
    basename = node["name"]
    wavedrom_src = node["src"]

    segments = split_waveform_if_needed(wavedrom_src, 50)
    image_dir = Path(self.builder.imagedir)

    segment_images = []
    
    for idx, segment in enumerate(segments):
        part_name = f"{basename}_part{idx+1}" if len(segments) > 1 else basename
        svg_filepath = Path(self.builder.outdir).joinpath(image_dir, f"{part_name}.svg")
        png_filepath = Path(self.builder.outdir).joinpath(image_dir, f"{part_name}.png")

        try:
            segment_svg = yowasp_wavedrom.render(segment) if len(segments) > 1 else yowasp_wavedrom.render(wavedrom_src)
            svg_filepath.parent.mkdir(parents=True, exist_ok=True)
            svg_filepath.write_text(segment_svg)
            adjust_svg(svg_filepath)
            cairosvg.svg2png(url=str(svg_filepath), write_to=str(png_filepath), dpi=300)
        except Exception as error:
            sphinx.application.logger.error(f"WaveDrom error: {error}")
            self.body.append(f'<em style="color:red;font-weight:bold">'
                             f'<pre>/!\\ WaveDrom Error: {self.encode(error)}</pre>'
                             f'</em>')
            raise nodes.SkipNode

        relative_png_path = f"{self.builder.imagedir}/{part_name}.png"
        segment_images.append(f'<img src="{relative_png_path}" class="wavedrom" alt="{self.encode(segment)}">')

    self.body.append("".join(segment_images))
    raise nodes.SkipNode

def latex_visit_wavedrom_diagram(self: LaTeXTranslator, node: nodes.Element):
    basename = node["name"]
    wavedrom_src = node["src"]

    segments = split_waveform_if_needed(wavedrom_src, MAX_WAVE_LENGTH)
    image_dir = Path(self.builder.imagedir)

    for idx, segment in enumerate(segments):
        if idx > 0 and not is_meaningful_segment(segment, segments[idx - 1]):
            continue  # Skip non-informative segments

        part_name = f"{basename}_part{idx+1}" if len(segments) > 1 else basename
        svg_filepath = Path(self.builder.outdir).joinpath(image_dir, f"{part_name}.svg")
        png_filepath = Path(self.builder.outdir).joinpath(image_dir, f"{part_name}.png")

        try:
            segment_svg = yowasp_wavedrom.render(segment)
            svg_filepath.parent.mkdir(parents=True, exist_ok=True)
            svg_filepath.write_text(segment_svg)
            adjust_svg(svg_filepath)
            cairosvg.svg2png(url=str(svg_filepath), write_to=str(png_filepath), dpi=300)
        except Exception as error:
            sphinx.application.logger.error(f"WaveDrom error: {error}")
            self.body.append(f"\\textbf{{WaveDrom Error: {error}}}")
            raise nodes.SkipNode

        relative_png_path = f"{image_dir}/{part_name}.png"
        self.body.append(
            f"\\begin{{figure}}[H]\\centering\\includegraphics[width=0.8\\textwidth]{{{relative_png_path}}}\\end{{figure}}"
        )
    
    raise nodes.SkipNode



def setup(app: sphinx.application.Sphinx):
    app.add_config_value("yowasp_wavedrom_skin", "default", "html", str)
    app.add_directive("wavedrom", WaveDromDirective)
    app.add_node(wavedrom_diagram,
        html=(html_visit_wavedrom_diagram, None),
        latex=(latex_visit_wavedrom_diagram, lambda self, node: None))
    return {
        "parallel_read_safe": True,
        "parallel_write_safe": True
    }
