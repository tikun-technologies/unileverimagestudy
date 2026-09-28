"""Visual system taken from the Rexona x MGA appeal report."""

from __future__ import annotations

from lxml import etree
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
    # Keep shapes behind text added later; no shadow.
    sp_pr = shape._element.spPr
    effect = sp_pr.find(qn("a:effectLst"))
    if effect is not None:
        sp_pr.remove(effect)
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
    tf.anchor = anchor
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
    tf.anchor = anchor
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
    tf.anchor = MSO_ANCHOR.MIDDLE
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


def add_footer(slide, note: str, page: int, total: int):
    add_rect(slide, 0.62, 6.98, 12.1, 0.01, PINK)
    add_text(slide, note, 0.62, 7.05, 10.4, 0.32, size=10, color=GRAY, anchor=MSO_ANCHOR.MIDDLE)
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
