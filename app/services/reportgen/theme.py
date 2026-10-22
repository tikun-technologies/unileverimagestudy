"""Visual system taken from the Rexona x MGA appeal report."""

from __future__ import annotations

from pathlib import Path

from lxml import etree
from PIL import ImageFont
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

FONT = "Karla"

SLIDE_W = 13.333333
SLIDE_H = 7.5

# Palette sampled from the source deck
NAVY = RGBColor(0x1E, 0x2D, 0x44)
NAVY_DEEP = RGBColor(0x1C, 0x25, 0x35)
INK = RGBColor(0x11, 0x11, 0x11)
GRAY = RGBColor(0x61, 0x70, 0x86)
GRAY_SOFT = RGBColor(0x6F, 0x7C, 0x8C)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
BLUSH = RGBColor(0xFF, 0xF6, 0xF8)
BLUSH_CARD = RGBColor(0xF8, 0xEA, 0xF1)
BLUSH_MID = RGBColor(0xF5, 0xE7, 0xEE)
PINK = RGBColor(0xEA, 0xC8, 0xD4)
PINK_DEEP = RGBColor(0xE7, 0xC8, 0xD6)
TEAL = RGBColor(0x04, 0x74, 0x6E)
TEAL_BRIGHT = RGBColor(0x07, 0x8C, 0x83)
GREEN = RGBColor(0x00, 0x89, 0x35)
RED = RGBColor(0xDD, 0x00, 0x00)
ROSE = RGBColor(0xC9, 0x6B, 0x84)
LINE = RGBColor(0xE7, 0xD5, 0xDE)
GRID = RGBColor(0xE2, 0xE2, 0xE2)


def rgb(value: str) -> RGBColor:
    return RGBColor.from_string(value)


def set_background(slide, color: RGBColor) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def add_rect(slide, x, y, w, h, fill: RGBColor, rounded: bool = False, line: RGBColor | None = None, radius: float = 0.08):
    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
    shape = slide.shapes.add_shape(shape_type, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = line
        shape.line.width = Pt(1)
    if rounded:
        try:
            shape.adjustments[0] = radius
        except Exception:
            pass
    # An empty effect list, plus a zero theme effect, stops the deck shadow.
    sp = shape._element
    sp_pr = sp.spPr
    effect = sp_pr.find(qn("a:effectLst"))
    if effect is not None:
        sp_pr.remove(effect)
    etree.SubElement(sp_pr, qn("a:effectLst"))
    style = sp.find(qn("p:style"))
    if style is not None:
        ref = style.find(qn("a:effectRef"))
        if ref is not None:
            ref.set("idx", "0")
    return shape


def add_text(
    slide,
    text: str,
    x,
    y,
    w,
    h,
    size: float = 14,
    bold: bool = False,
    color: RGBColor = NAVY,
    align=PP_ALIGN.LEFT,
    anchor=MSO_ANCHOR.TOP,
    italic: bool = False,
):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    tf.vertical_anchor = anchor
    parts = str(text if text is not None else "").split("\n")
    for index, part in enumerate(parts):
        paragraph = tf.paragraphs[0] if index == 0 else tf.add_paragraph()
        paragraph.alignment = align
        paragraph.space_after = Pt(4)
        run = paragraph.add_run()
        run.text = part
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = color
        run.font.name = FONT
    return box


def add_lines(slide, lines, x, y, w, h, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    """lines: sequence of (text, size, bold, color)."""
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    tf.vertical_anchor = anchor
    for i, (text, size, bold, color) in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(2)
        run = p.add_run()
        run.text = "" if text is None else str(text)
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        run.font.name = FONT
    return box


def lift_color(value: float) -> RGBColor:
    if value > 0.05:
        return GREEN
    if value < -0.05:
        return RED
    return GRAY


def bar_color(value: float) -> RGBColor:
    if value > 0.05:
        return TEAL
    if value < -0.05:
        return ROSE
    return GRAY_SOFT


def fmt_lift(value: float | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    if abs(number - round(number)) < 0.05:
        number = int(round(number))
        return f"+{number}" if number > 0 else str(number)
    return f"{number:+.1f}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{int(round(float(value)))}%"


def _set_run_font(run, size, color, bold=False):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = FONT


def style_cell(
    cell,
    text,
    size=11,
    bold=False,
    color=NAVY,
    fill: RGBColor | None = None,
    align=PP_ALIGN.LEFT,
    border: str = "F0E4EA",
    border_width: str = "6350",
):
    cell.text = ""
    tf = cell.text_frame
    tf.word_wrap = True
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = "" if text is None else str(text)
    _set_run_font(run, size, color, bold)
    cell.margin_left = Inches(0.08)
    cell.margin_right = Inches(0.06)
    cell.margin_top = Inches(0.04)
    cell.margin_bottom = Inches(0.04)
    if fill is None:
        cell.fill.background()
    else:
        cell.fill.solid()
        cell.fill.fore_color.rgb = fill
    _set_cell_border(cell, border, border_width)


def _set_cell_border(cell, color_hex: str, width: str = "6350"):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    for edge in ("lnL", "lnR", "lnT", "lnB"):
        for existing in tc_pr.findall(qn(f"a:{edge}")):
            tc_pr.remove(existing)
        ln = etree.SubElement(tc_pr, qn(f"a:{edge}"), w=width, cap="flat", cmpd="sng", algn="ctr")
        solid = etree.SubElement(ln, qn("a:solidFill"))
        etree.SubElement(solid, qn("a:srgbClr"), val=color_hex)
        etree.SubElement(ln, qn("a:prstDash"), val="solid")


# Leading used when a heading is sized to a box. 1.15 keeps one line inside the measured height.
_LEADING = 1.15
_FONT_CACHE: dict[tuple[bool, float], ImageFont.FreeTypeFont] = {}


def _karla(size: float, bold: bool) -> ImageFont.FreeTypeFont | None:
    key = (bold, round(float(size) * 2) / 2)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    filename = "Karla-Bold.ttf" if bold else "Karla-Regular.ttf"
    path = Path(__file__).resolve().parent / "fonts" / filename
    if not path.is_file():
        return None
    font = ImageFont.truetype(str(path), size=key[1])
    _FONT_CACHE[key] = font
    return font


def _text_width(text: str, size: float, bold: bool) -> float:
    """Width in inches. A small margin keeps PowerPoint from wrapping a line we measured as fitting."""
    font = _karla(size, bold)
    if font is not None:
        return font.getlength(text) / 72.0 * 1.04
    factor = 0.56 if bold else 0.52
    return len(text) * size * factor / 72.0 * 1.04


def _wrap_count(text: str, width: float, size: float, bold: bool) -> int:
    usable = max(width - 0.08, 0.4)
    words = str(text).split()
    if not words:
        return 1
    lines = 1
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if _text_width(trial, size, bold) <= usable:
            current = trial
            continue
        if current:
            lines += 1
        current = word
    return lines


def block_height(lines: int, size: float) -> float:
    return max(1, lines) * size * _LEADING / 72.0 + 0.02


def fitted_size(
    text: str,
    width: float,
    max_height: float,
    preferred: float,
    minimum: float = 9,
    bold: bool = False,
    max_lines: int = 2,
    one_line_floor: float | None = None,
) -> tuple[float, float]:
    """Largest font that keeps the text inside the box. Returns size and the height it needs.

    When ``one_line_floor`` is set, a single line is kept if it can stay at or above that size.
    """
    body = "" if text is None else str(text)
    if not body.strip():
        return float(preferred), 0.0
    if one_line_floor is not None:
        size = float(preferred)
        while size >= float(one_line_floor) - 0.01:
            if _wrap_count(body, width, size, bold) <= 1 and block_height(1, size) <= max_height + 0.015:
                return size, block_height(1, size)
            size -= 0.5
    size = float(preferred)
    floor = float(minimum)
    while size >= floor - 0.01:
        lines = _wrap_count(body, width, size, bold)
        height = block_height(lines, size)
        if lines <= max_lines and height <= max_height + 0.015:
            return size, height
        size -= 0.5
    lines = min(_wrap_count(body, width, floor, bold), max_lines)
    return floor, min(block_height(max(lines, 1), floor), max_height)


def apply_leading(box, size: float, factor: float = _LEADING) -> None:
    """Lock line spacing to the same leading used to measure the fit."""
    for paragraph in box.text_frame.paragraphs:
        properties = paragraph._p.get_or_add_pPr()
        for node in list(properties.findall(qn("a:lnSpc"))):
            properties.remove(node)
        spacing = etree.SubElement(properties, qn("a:lnSpc"))
        points = etree.SubElement(spacing, qn("a:spcPts"))
        points.set("val", str(int(round(size * factor * 100))))


def add_footer(slide, note: str, page: int, total: int):
    add_rect(slide, 0.62, 6.98, 12.1, 0.01, PINK)
    size, _height = fitted_size(note, 10.3, 0.28, preferred=10, minimum=8, bold=False, max_lines=1)
    note_box = add_text(slide, note, 0.62, 7.05, 10.4, 0.30, size=size, color=GRAY, anchor=MSO_ANCHOR.MIDDLE)
    apply_leading(note_box, size)
    add_text(
        slide,
        str(page),
        11.7,
        7.05,
        1.0,
        0.32,
        size=11,
        bold=True,
        color=NAVY,
        align=PP_ALIGN.RIGHT,
        anchor=MSO_ANCHOR.MIDDLE,
    )
    # total is accepted so callers can show "n / N" later without changing the signature.
    _ = total


def emu(inches: float) -> Emu:
    return Inches(inches)
