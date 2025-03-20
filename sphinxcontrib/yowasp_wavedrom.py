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
MAX_WAVE_LENGTH = 15  # Only split if the signal length exceeds this limit
FONT_SIZE = "15px"  # Adjusted smaller font size

class WaveDromDirective(Directive):
    required_arguments = 1
    has_content = True

    def run(self):
        self.assert_has_content()
        name, = self.arguments

        # Extract payload from the directive block
        payload = re.sub(r"^..\s+wavedrom\s*::.+?\n", "\n", self.block_text)

        # Parse and validate WaveJSON
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
    """Ensures consistent font size, sets text to dark blue, and signal lines to black."""
    try:
        tree = etree.parse(svg_filepath)
        root = tree.getroot()

        namespaces = {'svg': 'http://www.w3.org/2000/svg'}

        # Modify all <text> elements (apply correct font size and set dark blue text color)
        for text_element in root.xpath('//svg:text', namespaces=namespaces):
            style = text_element.get("style", "")

            # Ensure font size is set inside style
            if "font-size" in style:
                style = re.sub(r"font-size:\s*\d+px", f"font-size: {font_size}", style)
            else:
                style += f"; font-size: {font_size};"

            # Ensure fill color is set inside style
            if "fill" in style:
                style = re.sub(r"fill:\s*#[0-9a-fA-F]{3,6}", "fill: #00008B", style)
            else:
                style += "; fill: #00008B;"

            text_element.set("style", style)
            text_element.set("font-size", font_size)  # Ensure text size
            text_element.set("fill", "#00008B")  # Force text to dark blue

        # Modify all <path> and <line> elements (signal lines) to ensure they are black
        for line_element in root.xpath('//svg:path | //svg:line', namespaces=namespaces):
            style = line_element.get("style", "")

            # Ensure stroke is black
            if "stroke" in style:
                style = re.sub(r"stroke:\s*#[0-9a-fA-F]{3,6}", "stroke: #000", style)
            else:
                style += "; stroke: #000;"

            line_element.set("style", style)
            line_element.set("stroke", "#000")  # Force signal lines to be black

        tree.write(str(svg_filepath), encoding="utf-8", xml_declaration=True)
    except Exception as e:
        sphinx.application.logger.error(f"Error adjusting SVG: {e}")

def split_waveform_if_needed(wavedrom_src, max_length):
    """Splits waveform into multiple segments, padding the last segment to the fixed length if needed."""
    signals = wavedrom_src.get("signal", [])
    max_wave_length = max((len(sig.get("wave", "")) for sig in signals), default=0)
    if max_wave_length <= max_length:
        return [wavedrom_src]  # No split needed

    num_segments = (max_wave_length + max_length - 1) // max_length
    segments = []

    for i in range(num_segments):
        segment = {"signal": [], "config": wavedrom_src.get("config", {}).copy()}
        for sig in signals:
            wave = sig.get("wave", "")
            # Extract the current segment
            wave_segment = wave[i * max_length: (i + 1) * max_length]
            # Pad the last segment with '.' so that its length equals max_length
            if len(wave_segment) < max_length:
                wave_segment = wave_segment.ljust(max_length, '.')
            segment["signal"].append({
                **sig,
                "wave": wave_segment
            })
        segments.append(segment)

    return segments



def html_visit_wavedrom_diagram(self: sphinx.writers.html5.HTML5Translator, node: wavedrom_diagram):
    basename = node["name"]
    wavedrom_src = node["src"]

    # Apply skin from Sphinx config
    wavedrom_src_config = wavedrom_src.setdefault("config", {})
    if "signal" in wavedrom_src:
        wavedrom_src_config.setdefault("skin", self.builder.config.yowasp_wavedrom_skin)

    # Render WaveJSON to SVG
    try:
        wavedrom_svg = yowasp_wavedrom.render(wavedrom_src)
    except Exception as error:
        sphinx.application.logger.error(f"Could not render WaveDrom: {error}")
        self.body.append(f'<em style="color:red;font-weight:bold">'
                         f'<pre>/!\\ WaveDrom Error: {self.encode(error)}</pre>'
                         f'</em>')
        raise nodes.SkipNode

    # Save SVG file
    pathname = Path(self.builder.outdir).joinpath(self.builder.imagedir, f'{basename}.svg')
    pathname.parent.mkdir(parents=True, exist_ok=True)
    pathname.write_text(wavedrom_svg)

    # **Fix SVG rendering issues**
    adjust_svg(pathname)

    # Convert SVG to PNG for better browser consistency
    png_filepath = pathname.with_suffix(".png")
    cairosvg.svg2png(url=str(pathname), write_to=str(png_filepath), dpi=300)

    # Reference PNG instead of SVG in HTML
    self.body.append(f'<img src="{self.builder.imagedir}/{basename}.png" '
                     f'class="wavedrom" alt="{self.encode(node["src"])}">')
    raise nodes.SkipNode



def latex_visit_wavedrom_diagram(self: LaTeXTranslator, node: nodes.Element):
    basename = node["name"]
    wavedrom_src = node["src"]

    segments = split_waveform_if_needed(wavedrom_src, MAX_WAVE_LENGTH)
    image_dir = Path(self.builder.imagedir)
    
    # Process each segment individually, outputting each in its own figure
    for idx, segment in enumerate(segments):
        part_name = f"{basename}_part{idx+1}" if len(segments) > 1 else basename
        svg_filepath = Path(self.builder.outdir).joinpath(image_dir, f"{part_name}.svg")
        png_filepath = Path(self.builder.outdir).joinpath(image_dir, f"{part_name}.png")
        try:
            # Use the correct source for the segment
            segment_svg = yowasp_wavedrom.render(segment) if len(segments) > 1 else yowasp_wavedrom.render(wavedrom_src)
            svg_filepath.parent.mkdir(parents=True, exist_ok=True)
            svg_filepath.write_text(segment_svg)
            adjust_svg(svg_filepath)
            cairosvg.svg2png(url=str(svg_filepath), write_to=str(png_filepath), dpi=300)
        except Exception as error:
            sphinx.application.logger.error(f"WaveDrom error: {error}")
            self.body.append(f"\\textbf{{WaveDrom Error: {error}}}")
            raise nodes.SkipNode
        relative_png_path = f"{image_dir}/{part_name}.png"
        # Use [H] to force the figure's placement (requires \usepackage{float} in your LaTeX preamble)
        self.body.append(
            f"\\begin{{figure}}[H]\\centering\\includegraphics[width=0.8\\textwidth]{{{relative_png_path}}}\\end{{figure}}"
        )
    
    raise nodes.SkipNode



# Setup function for Sphinx
def setup(app: sphinx.application.Sphinx):
    app.add_config_value("yowasp_wavedrom_skin", "default", "html", str)
    app.add_directive("wavedrom", WaveDromDirective)
    app.add_node(wavedrom_diagram,
        html=(html_visit_wavedrom_diagram, None),
        latex=(latex_visit_wavedrom_diagram, None))  
    return {
        "parallel_read_safe": True,
        "parallel_write_safe": True
    }