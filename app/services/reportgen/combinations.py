"""Design Combination Readout driven by saved combination groups.

The slide order follows the Rexona x MGA combination deck: cover, how to
read lifts, then a section for each metric that appears in the file (Top Down,
Bottom Up, Response Time). Inside a section, each saved group is its own
segment: a divider, a comparison chart, and one detail slide per design.
"""

from __future__ import annotations

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION
from pptx.enum.shapes import MSO_CONNECTOR
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

from app.services.reportgen.composite import composite_pack
from app.services.reportgen.theme import (
    BLUSH,
    FONT,
    GRAY,
    GREEN,
    NAVY,
    PINK,
    RED,
    TEAL,
    WHITE,
    add_rect,
    add_text,
    set_background,
)

ASSETS = Path(__file__).resolve().parent / "template_assets"
SLIDE_W = 13.333333
SLIDE_H = 7.5
RAIL = RGBColor(0xF3, 0xC9, 0xD4)
INK = RGBColor(0x17, 0x22, 0x38)
CARD = RGBColor(0xFB, 0xEF, 0xF2)
TRACK = RGBColor(0xF4, 0xF0, 0xF2)

TOP_RANKS = (
    "PACK 1 — HIGHEST LIFT BUILD",
    "PACK 2 — SECOND HIGHEST LIFT BUILD",
    "PACK 3 — THIRD HIGHEST LIFT BUILD",
)
BOTTOM_RANKS = (
    "BOTTOM PACK 1 — LOWEST LIFT BUILD",
    "BOTTOM PACK 2",
    "BOTTOM PACK 3",
)
RESPONSE_RANKS = ("PACK 1", "PACK 2", "PACK 3")

_KIND = {
    "top": {
        "family": "TOP COMBINATIONS",
        "prefix": "TOP 3 PACK COMBINATIONS",
        "chip": "TOP COMBINATION",
        "ranks": TOP_RANKS,
        "metric": "Top Down",
        "base_note": "starting point (overall)",
    },
    "bottom": {
        "family": "BOTTOM COMBINATIONS",
        "prefix": "BOTTOM 3",
        "chip": "BOTTOM COMBINATION",
        "ranks": BOTTOM_RANKS,
        "metric": "Bottom Up",
        "base_note": "starting point (bottom up)",
    },
    "response": {
        "family": "RESPONSE TIME",
        "prefix": "RESPONSE TIME",
        "chip": "RESPONSE TIME",
        "ranks": RESPONSE_RANKS,
        "metric": "Response Time",
        "base_note": "starting point (response time)",
    },
}


def _metric_kind(metric: str) -> str:
    text = metric.lower()
    if "bottom" in text:
        return "bottom"
    if "response" in text:
        return "response"
    return "top"


def _digits(kind: str) -> int:
    return 3 if kind == "response" else 1


def _optional_float(value) -> float | None:
    if value is None:
        return None
    return float(value)


def _base_for(study: StudyInfo, kind: str) -> float | None:
    if kind == "bottom":
        return study.base_bottom
    if kind == "response":
        return study.base_response
    return study.base_appeal


@dataclass
class Element:
    name: str
    label: str
    lift: float
    category: str
    z_index: int
    image_url: str | None = None
    image_path: Path | None = None


@dataclass
class Build:
    position: int
    name: str
    elements: list[Element]
    total: float
    metric: str = "Top Down"
    background_url: str | None = None
    background_path: Path | None = None

    def signature(self) -> tuple[str, ...]:
        return tuple(element.name for element in self.elements)


@dataclass
class Group:
    name: str
    position: int
    segment: str
    metric: str
    builds: list[Build] = field(default_factory=list)

    def builds_for(self, kind: str) -> list[Build]:
        return [build for build in self.builds if _metric_kind(build.metric) == kind]


@dataclass
class StudyInfo:
    title: str
    country: str
    launched_label: str
    respondents: int
    base_appeal: float | None
    base_bottom: float | None
    base_response: float | None
    model_name: str
    aspect: float
    segment_sizes: dict[str, int] = field(default_factory=dict)


def build_combination_readout(
    combinations_path: str | Path,
    study_path: str | Path,
    output_path: str | Path,
    analysis_path: str | Path | None = None,
    download_images: bool = True,
) -> Path:
    groups = load_groups(combinations_path)
    study = load_study(study_path, analysis_path)
    if download_images:
        cache = Path(combinations_path).parent / ".cache" / "elements"
        _download(groups, cache)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    presentation = Presentation()
    presentation.slide_width = Inches(SLIDE_W)
    presentation.slide_height = Inches(SLIDE_H)
    presentation.core_properties.title = f"Design Combination Readout v2 — {study.title}"
    presentation.core_properties.subject = "Design Combination Readout"
    presentation.core_properties.category = study.model_name

    sections = (
        ("top", "Top 3 Performing\nCombinations"),
        ("bottom", "Bottom 3 Performing\nCombinations"),
        ("response", "Response Time\nCombinations"),
    )
    slides: list[tuple[str, object]] = []
    slides.append(("plain", _cover(presentation, study)))
    slides.append(("content", _how_to_read(presentation, study, groups)))
    for kind, title in sections:
        blocks = [(group, group.builds_for(kind)) for group in groups]
        blocks = [(group, builds) for group, builds in blocks if builds]
        if not blocks:
            continue
        slides.append(("plain", _section(presentation, title)))
        for group, builds in blocks:
            slides.extend(_group_slides(presentation, study, group, builds, kind))
    slides.append(("plain", _thanks(presentation, study)))

    for index, (kind, slide) in enumerate(slides, start=1):
        if kind == "content":
            _page_number(slide, index)
    presentation.save(str(destination))
    return destination


def load_groups(path: str | Path) -> list[Group]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_groups = _as_groups(payload)
    groups: list[Group] = []
    for raw in raw_groups:
        items = list(raw.get("items") or [])
        items.sort(key=lambda item: int(item.get("position") or 0))
        builds = [_build_from_item(item) for item in items]
        if not builds:
            continue
        first = items[0]
        item_segment = str(first.get("segment_label") or (first.get("configuration") or {}).get("segment", {}).get("label") or "")
        # The group name is the saved segment (often a classification filter).
        # Designs inside it still say Overall when the scores come from that model.
        segment = str(raw.get("name") or item_segment or "Overall")
        groups.append(
            Group(
                name=segment,
                position=int(raw.get("position") or 0),
                segment=segment,
                metric=str(first.get("metric") or "Top Down"),
                builds=builds,
            )
        )
    groups.sort(key=lambda group: group.position)
    if not groups:
        raise ValueError("No saved combination groups were found.")
    return groups


def load_study(study_path: str | Path, analysis_path: str | Path | None = None) -> StudyInfo:
    study = json.loads(Path(study_path).read_text(encoding="utf-8"))
    config = study.get("study_config") or {}
    analysis = _load_analysis(study_path, analysis_path, str(study.get("id") or ""))
    front = (analysis or {}).get("Front Page") or {}
    overall = (analysis or {}).get("(T) Overall") or {}
    intercepts = (analysis or {}).get("(T) Intercepts") or {}
    bottom_intercepts = (analysis or {}).get("(B) Intercepts") or {}
    response_intercepts = (analysis or {}).get("(R) Intercepts") or {}
    dashboard = (analysis or {}).get("dashboard_summary") or {}
    settings = (analysis or {}).get("analysis_settings") or {}
    bands = ((settings.get("top") or {}).get("hundred") or [4, 5])
    raw_intercept = intercepts.get("intercept")
    sizes = {"Overall": int(overall.get("base_size") or dashboard.get("totalRespondents") or config.get("number_of_respondents") or 0)}
    gender = ((analysis or {}).get("(T) Gender") or {}).get("segments") or {}
    for label, info in gender.items():
        if isinstance(info, dict) and info.get("base_size"):
            sizes[str(label)] = int(info["base_size"])
    return StudyInfo(
        title=str(front.get("Title") or study.get("title") or "Design Study"),
        country=str(config.get("country") or "Overall"),
        launched_label=_launched(front.get("Launched At") or study.get("created_at")),
        respondents=sizes["Overall"],
        base_appeal=float(raw_intercept) if raw_intercept is not None else None,
        base_bottom=_optional_float(bottom_intercepts.get("intercept")),
        base_response=_optional_float(response_intercepts.get("intercept")),
        model_name=f"Top-{len(bands)} Box" if len(bands) > 1 else "Top Box",
        aspect=_aspect(config.get("aspect_ratio") or front.get("Aspect Ratio") or "9:16"),
        segment_sizes=sizes,
    )


def _as_groups(payload) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("groups") or payload.get("combinations") or payload.get("items") or []
    if not isinstance(payload, list):
        raise ValueError("Saved combinations JSON must be a list of groups.")
    if payload and isinstance(payload[0], dict) and "items" in payload[0]:
        return payload
    return [{"name": "Overall", "position": 0, "items": payload}]


def _build_from_item(item: dict) -> Build:
    config = item.get("configuration") or {}
    elements = []
    for raw in config.get("selected_elements") or []:
        name = str(raw.get("name") or "")
        category = str(raw.get("category") or "")
        elements.append(
            Element(
                name=name,
                label=_pretty(name),
                lift=float(raw.get("value") or 0),
                category=category,
                z_index=int(raw.get("z_index") or 0),
                image_url=raw.get("image_url") or raw.get("content"),
            )
        )
    elements.sort(key=lambda element: element.z_index)
    total = item.get("total_coefficient")
    if total is None:
        total = sum(element.lift for element in elements)
    return Build(
        position=int(item.get("position") or 0),
        name=str(item.get("name") or ""),
        elements=elements,
        total=float(total),
        metric=str(item.get("metric") or config.get("metric") or "Top Down"),
        background_url=config.get("background_url"),
    )


def _load_analysis(study_path: str | Path, analysis_path: str | Path | None, study_id: str) -> dict | None:
    candidates = []
    if analysis_path:
        candidates.append(Path(analysis_path))
    study = Path(study_path)
    candidates.append(study.with_name("analysis_data.json"))
    candidates.append(study.parent.parent / "Dafault-export" / "fresh-harvest" / "analysis_data.json")
    for candidate in candidates:
        if not candidate.exists():
            continue
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        if study_id and payload.get("study_id") and payload.get("study_id") != study_id:
            continue
        analysis = payload.get("analysis")
        if isinstance(analysis, dict):
            return analysis
    return None


def _pretty(name: str) -> str:
    text = name.strip()
    for prefix in (
        "Background Layer ",
        "HEADLINE - ",
        "Headline - ",
        "Sub-Header - ",
        "Sub Header - ",
        "Image Layer_ ",
        "Image Layer ",
        "CTA - ",
    ):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return re.sub(r"\s+", " ", text).strip() or name


def _aspect(value: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)", str(value))
    if not match:
        return 9 / 16
    width, height = float(match.group(1)), float(match.group(2))
    if height == 0:
        return 9 / 16
    return width / height


def _launched(value) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.strftime("%B %Y")


def _download(groups: list[Group], cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, Path, str]] = []
    seen: set[str] = set()

    def add(url: str | None):
        if not url or url in seen:
            return
        seen.add(url)
        leaf = url.rstrip("/").split("/")[-1]
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf)[:120] or "asset"
        if not safe.lower().endswith(".png"):
            safe = f"{safe}.png"
        jobs.append((url, cache / safe, url))

    for group in groups:
        for build in group.builds:
            add(build.background_url)
            for element in build.elements:
                add(element.image_url)

    def fetch(item: tuple[str, Path, str]):
        key, dest, url = item
        if dest.exists() and dest.stat().st_size > 0:
            return key, dest
        request = urllib.request.Request(quote(url, safe=":/?#[]@!$&'()*+,;=%"), headers={"User-Agent": "presentation-generator"})
        with urllib.request.urlopen(request, timeout=40) as response:
            dest.write_bytes(response.read())
        return key, dest

    saved: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, job) for job in jobs]
        for future in as_completed(futures):
            try:
                key, dest = future.result()
            except Exception as exc:
                print(f"  image skipped: {exc}")
                continue
            saved[key] = dest
    for group in groups:
        for build in group.builds:
            if build.background_url:
                build.background_path = saved.get(build.background_url)
            for element in build.elements:
                if element.image_url:
                    element.image_path = saved.get(element.image_url)


def _group_slides(presentation, study: StudyInfo, group: Group, builds: list[Build], kind: str):
    meta = _KIND[kind]
    slides = [("plain", _market(presentation, group, kind))]
    slides.append(("content", _compare(presentation, study, group, builds, kind)))
    for index, build in enumerate(builds):
        headline = meta["ranks"][index] if index < len(meta["ranks"]) else f"PACK {index + 1}"
        slides.append(("content", _pack(presentation, study, group, build, headline, kind)))
    return slides


def _blank(presentation):
    return presentation.slides.add_slide(presentation.slide_layouts[6])


def _flush(box):
    frame = box.text_frame
    frame.margin_left = Emu(0)
    frame.margin_right = Emu(0)
    frame.margin_top = Emu(0)
    frame.margin_bottom = Emu(0)
    return box


def _text(slide, text, x, y, w, h, **kwargs):
    return _flush(add_text(slide, text, x, y, w, h, **kwargs))


def _hairline(slide, x1, y1, x2, y2, color: RGBColor = RGBColor(0xF2, 0xC6, 0xCF), width: float = 0.75):
    line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    line.line.color.rgb = color
    line.line.width = Pt(width)
    return line


def _rail(slide):
    add_rect(slide, 0, 0, 0.47, SLIDE_H, RAIL)


def _page_number(slide, number: int):
    _text(slide, str(number), 12.45, 7.16, 0.7, 0.22, size=11, bold=True, color=NAVY, align=PP_ALIGN.RIGHT)


def _report_line(study: StudyInfo) -> str:
    return f"{study.title}  |  {study.launched_label}".strip(" |")


def _marks(slide):
    mga = ASSETS / "mga_logo.png"
    _text(slide, "MGA", 1.15, 2.45, 2.4, 0.5, size=28, bold=True, color=NAVY, align=PP_ALIGN.CENTER)
    if mga.exists():
        slide.shapes.add_picture(str(mga), Inches(1.377), Inches(3.55), Inches(1.795), Inches(1.795))


def _cover(presentation, study: StudyInfo):
    slide = _blank(presentation)
    set_background(slide, BLUSH)
    _marks(slide)
    _hairline(slide, 4.2464, 1.3478, 4.2464, 1.3478 + 4.7826)
    _hairline(slide, 4.5996, 3.75, 4.5996 + 6.8551, 3.75)
    _text(slide, "Design Combination Readout v2", 4.60, 1.98, 8.48, 0.85, size=32, bold=True, color=NAVY)
    _text(slide, _report_line(study), 4.60, 4.24, 8.2, 0.37, size=16, color=NAVY)
    _text(
        slide,
        "Prepared by: J Brown Fitterman | jbrown@mindgenomicsassociates.com",
        4.60, 4.63, 7.64, 0.37, size=16, color=NAVY,
    )
    _text(slide, "Mind Genomics Associates, Inc", 4.60, 5.01, 4.2, 0.37, size=16, color=NAVY)
    _text(slide, study.country, 4.60, 5.71, 8.0, 0.44, size=16, color=NAVY)
    return slide


def _thanks(presentation, study: StudyInfo):
    slide = _blank(presentation)
    set_background(slide, BLUSH)
    _marks(slide)
    _hairline(slide, 4.2464, 1.3478, 4.2464, 1.3478 + 4.7826)
    _hairline(slide, 4.5996, 3.75, 4.5996 + 6.8551, 3.75)
    _text(slide, "Thank you", 4.60, 1.98, 7.8, 0.7, size=36, bold=True, color=NAVY)
    _text(slide, "We look forward to exploring more combinations", 4.61, 2.82, 7.6, 0.4, size=16, color=NAVY)
    _text(slide, _report_line(study), 4.60, 4.24, 8.2, 0.37, size=16, color=NAVY)
    _text(
        slide,
        "Prepared by: J Brown Fitterman | jbrown@mindgenomicsassociates.com",
        4.60, 4.63, 7.64, 0.37, size=16, color=NAVY,
    )
    _text(slide, "Mind Genomics Associates, Inc", 4.60, 5.01, 4.2, 0.37, size=16, color=NAVY)
    _text(slide, study.country, 4.60, 5.71, 8.0, 0.44, size=16, color=NAVY)
    return slide


def _section(presentation, title: str):
    slide = _blank(presentation)
    set_background(slide, BLUSH)
    add_rect(slide, 0, 0, 0.18, SLIDE_H, PINK)
    _text(slide, title, 1.15, 2.15, 11.4, 3.2, size=48, bold=True, color=NAVY)
    return slide


def _market(presentation, group: Group, kind: str):
    slide = _blank(presentation)
    set_background(slide, BLUSH)
    _text(
        slide, _KIND[kind]["metric"],
        0.9, 2.05, 11.5, 0.4,
        size=16, bold=True, color=TEAL, align=PP_ALIGN.CENTER,
    )
    size = 26 if len(group.segment) > 42 else 36
    _text(
        slide, group.segment,
        0.9, 2.55, 11.5, 2.3,
        size=size, bold=True, color=NAVY, align=PP_ALIGN.CENTER,
    )
    return slide


def _how_to_read(presentation, study: StudyInfo, groups: list[Group]):
    slide = _blank(presentation)
    set_background(slide, WHITE)
    _rail(slide)
    _text(slide, "HOW TO READ THIS REPORT: UNDERSTANDING ELEMENT LIFTS", 0.83, 0.33, 12.0, 0.28, size=14, bold=True, color=TEAL)
    add_rect(slide, 0.83, 0.68, 12.0, 0.46, CARD, rounded=True, radius=0.08)
    _text(
        slide,
        "Top builds are created by selecting the strongest option in each element category",
        0.98, 0.74, 11.7, 0.34, size=15, bold=True, color=NAVY, anchor=MSO_ANCHOR.MIDDLE,
    )
    add_rect(slide, 0.83, 1.32, 6.05, 5.45, WHITE, rounded=True, radius=0.04, line=RGBColor(0xF0, 0xE4, 0xEA))
    _text(slide, "How the model works", 1.05, 1.48, 5.6, 0.32, size=16, bold=True, color=NAVY)
    _text(
        slide,
        (
            "Respondents rated a large number of sign combinations — each showing different design elements. "
            "The model then separates those ratings into an individual lift score.\n\n"
            "Each lift score shows how much that element raises or lowers appeal relative to the model baseline.\n\n"
            "A saved combination keeps one option from each layer. Swapping one element only changes that slot. "
            "Everything else stays, because each score was measured on its own."
        ),
        1.05, 1.90, 5.6, 4.5, size=14, color=INK,
    )
    add_rect(slide, 7.08, 1.32, 5.75, 5.45, CARD, rounded=True, radius=0.04)
    add_rect(slide, 7.08, 1.32, 5.75, 0.1, TEAL)
    _text(slide, "Example from the saved builds", 7.28, 1.55, 5.35, 0.4, size=16, bold=True, color=NAVY)
    _text(slide, _example_copy(groups), 7.28, 2.15, 5.35, 4.3, size=14, color=INK)
    return slide


def _example_copy(groups: list[Group]) -> str:
    pair = _swap_example(groups)
    if pair is None:
        return "Each row in the build slides is one layer. The number beside it is that element’s lift. The total is the sum of the selected lifts."
    group, higher, lower, index = pair
    changed_high = higher.elements[index]
    changed_low = lower.elements[index]
    return (
        f"In {group.segment}, one build uses {changed_high.label} ({_lift(changed_high.lift)}) in {changed_high.category}.\n\n"
        f"Swapping to {changed_low.label} ({_lift(changed_low.lift)}) only changes that slot. "
        f"The other layers stay as they are.\n\n"
        f"The result: {changed_low.label} totals {_lift(lower.total)} against {_lift(higher.total)} "
        f"for {changed_high.label} — a difference of {abs(higher.total - lower.total):.1f} points."
    )


def _swap_example(groups: list[Group]):
    found = None
    for group in groups:
        for left_index, left in enumerate(group.builds):
            for right in group.builds[left_index + 1:]:
                if _metric_kind(left.metric) != _metric_kind(right.metric):
                    continue
                if len(left.elements) != len(right.elements):
                    continue
                diffs = [i for i, (a, b) in enumerate(zip(left.elements, right.elements)) if a.name != b.name]
                if len(diffs) != 1:
                    continue
                gap = abs(left.total - right.total)
                higher, lower = (left, right) if left.total >= right.total else (right, left)
                candidate = (gap > 0, gap, group, higher, lower, diffs[0])
                if found is None or candidate[:2] > found[:2]:
                    found = candidate
    if found is None:
        return None
    _, _, group, higher, lower, index = found
    return group, higher, lower, index


_BAR_COLORS = (
    RGBColor(0x15, 0x5E, 0x75),
    RGBColor(0xD4, 0x63, 0x2A),
    RGBColor(0x0F, 0x76, 0x6E),
    RGBColor(0xC4, 0x5C, 0x26),
    RGBColor(0x1D, 0x4E, 0x89),
    RGBColor(0xB4, 0x53, 0x09),
)


def _compare(presentation, study: StudyInfo, group: Group, builds: list[Build], kind: str):
    """Column chart plus a thumbnail row, matching the Rexona variance slide."""
    slide = _blank(presentation)
    set_background(slide, WHITE)
    _rail(slide)
    meta = _KIND[kind]
    _text(
        slide,
        f"KEY FINDINGS: {meta['family']}",
        0.83, 0.36, 7.6, 0.22, size=12, bold=True, color=TEAL,
    )
    title = _text(
        slide, group.segment,
        0.83, 0.60, 7.6, 0.62, size=16, bold=True, color=NAVY,
    )
    title.text_frame.word_wrap = True
    _hairline(slide, 0.83, 1.28, 2.28, 1.28, TEAL, 1.5)
    _text(slide, meta["chip"], 0.83, 1.36, 4.2, 0.22, size=11, bold=True, color=GRAY)

    _variance_chart(slide, builds, kind, 0.55, 1.68, 7.7, 5.05)
    _thumbnail_panel(slide, study, builds, kind, 8.50, 0.95, 4.52, 5.75)
    note = _text(slide, _sample_line(study, group, kind), 0.75, 7.05, 12.0, 0.36, size=9, color=GRAY)
    note.text_frame.word_wrap = True
    return slide


def _variance_chart(slide, builds: list[Build], kind: str, x, y, w, h):
    digits = _digits(kind)
    data = CategoryChartData()
    data.categories = [_chart_label(_variant_label(builds, build)) for build in builds]
    data.add_series("Lift above baseline", [build.total for build in builds])
    chart = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(x), Inches(y), Inches(w), Inches(h), data
    ).chart
    chart.has_legend = False
    chart.has_title = False
    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.gap_width = 80
    labels = plot.data_labels
    labels.font.size = Pt(11)
    labels.font.bold = True
    labels.font.name = FONT
    labels.font.color.rgb = NAVY
    labels.number_format = "0.000" if digits == 3 else "0.0"
    labels.position = XL_LABEL_POSITION.OUTSIDE_END

    series = chart.series[0]
    for index, point in enumerate(series.points):
        point.format.fill.solid()
        point.format.fill.fore_color.rgb = _BAR_COLORS[index % len(_BAR_COLORS)]

    peak = max((build.total for build in builds), default=4)
    headroom = max(peak * 0.28, 0.25 if peak < 5 else 4)
    axis_max = _nice_ceiling(peak + headroom)
    value_axis = chart.value_axis
    value_axis.minimum_scale = 0
    value_axis.maximum_scale = axis_max
    value_axis.major_unit = _axis_step(axis_max)
    value_axis.has_major_gridlines = True
    value_axis.major_gridlines.format.line.color.rgb = RGBColor(0xE2, 0xE2, 0xE2)
    value_axis.has_title = True
    title = value_axis.axis_title.text_frame.paragraphs[0]
    title.text = "Estimated total lift above baseline"
    title.font.size = Pt(10)
    title.font.bold = True
    title.font.name = FONT
    title.font.color.rgb = NAVY
    value_axis.tick_labels.font.size = Pt(9)
    value_axis.tick_labels.font.name = FONT
    value_axis.tick_labels.font.color.rgb = GRAY
    value_axis.format.line.color.rgb = RGBColor(0xB9, 0xC1, 0xCA)

    category_axis = chart.category_axis
    category_axis.has_major_gridlines = False
    category_axis.tick_labels.font.size = Pt(10)
    category_axis.tick_labels.font.name = FONT
    category_axis.tick_labels.font.color.rgb = GRAY
    return chart


def _thumbnail_panel(slide, study: StudyInfo, builds: list[Build], kind: str, x, y, w, h):
    digits = _digits(kind)
    add_rect(slide, x, y, w, h, CARD, rounded=True, radius=0.04)
    cols = min(len(builds), 3) or 1
    rows = (len(builds) + cols - 1) // cols
    cell_w = w / cols
    cell_h = h / rows
    for index, build in enumerate(builds):
        col = index % cols
        row = index // cols
        cell_x = x + col * cell_w
        cell_y = y + row * cell_h
        image = _render(study, build)
        max_w = cell_w - 0.18
        thumb_h = min(cell_h - 1.05, max_w / study.aspect)
        thumb_w = thumb_h * study.aspect
        block_h = thumb_h + 0.78
        left = cell_x + (cell_w - thumb_w) / 2
        top = cell_y + max(0.14, (cell_h - block_h) / 2)
        if image:
            slide.shapes.add_picture(str(image), Inches(left), Inches(top), Inches(thumb_w), Inches(thumb_h))
        label_top = top + thumb_h + 0.08
        _text(
            slide, _variant_label(builds, build),
            cell_x + 0.06, label_top, cell_w - 0.12, 0.40,
            size=10, color=NAVY, align=PP_ALIGN.CENTER,
        )
        _text(
            slide, _lift(build.total, digits),
            cell_x + 0.06, label_top + 0.34, cell_w - 0.12, 0.28,
            size=14, bold=True, color=GREEN, align=PP_ALIGN.CENTER,
        )


def _chart_label(label: str) -> str:
    if " · " in label and len(label) > 22:
        return label.replace(" · ", "\n", 1)
    return label


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1
    if value <= 2:
        step = 0.2
    elif value <= 8:
        step = 1
    elif value <= 40:
        step = 4
    else:
        step = 10
    count = int(value / step)
    if abs(value - count * step) > 1e-9:
        count += 1
    return round(step * count, 3)


def _axis_step(axis_max: float) -> float:
    if axis_max <= 2:
        return 0.4
    if axis_max <= 8:
        return 1
    return 4 if axis_max <= 40 else 10


# Colors sampled from the Rexona combination detail slide.
_INK = RGBColor(0x1A, 0x2A, 0x40)
_TITLE = RGBColor(0x0B, 0x1F, 0x4B)
_EYEBROW = RGBColor(0x1C, 0x25, 0x35)
_SEGMENT = RGBColor(0x02, 0x80, 0x90)
_MUTED = RGBColor(0x8A, 0x9B, 0xB0)
_BAR = RGBColor(0xF4, 0xE0, 0xE7)
_RULE = RGBColor(0xD0, 0xD8, 0xE4)
_PILL_UP = RGBColor(0x1A, 0x7A, 0x4A)
_PILL_DOWN = RGBColor(0xCC, 0x33, 0x00)
_RAIL_BLUSH = RGBColor(0xF5, 0xE7, 0xEE)


def _pack(presentation, study: StudyInfo, group: Group, build: Build, headline: str, kind: str):
    """Detail slide locked to the Rexona pack layout, with a 9:16 sign on the right."""
    slide = _blank(presentation)
    set_background(slide, WHITE)
    add_rect(slide, 0, 0, 0.47, SLIDE_H, _RAIL_BLUSH)

    meta = _KIND[kind]
    digits = _digits(kind)
    _text(slide, f"A-FRAME KEY FINDINGS: {meta['family']}", 0.83, 0.38, 8.2, 0.22, size=11, bold=True, color=_EYEBROW)
    add_rect(slide, 0.83, 0.68, 8.20, 0.36, _BAR)
    _text(slide, f"{meta['prefix']} · {headline}", 0.95, 0.68, 6.05, 0.36, size=11, bold=True, color=_TITLE, anchor=MSO_ANCHOR.MIDDLE)
    _text(
        slide, meta["metric"],
        7.05, 0.68, 1.85, 0.36,
        size=11, bold=True, color=_SEGMENT, align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE,
    )

    base_value = _base_for(study, kind)
    base = f"{base_value:.{digits}f}" if base_value is not None else "—"
    _text(slide, "Base appeal", 0.83, 1.18, 1.60, 0.38, size=11, bold=True, color=_INK, anchor=MSO_ANCHOR.MIDDLE)
    _text(slide, base, 2.52, 1.10, 1.35, 0.50, size=22, bold=True, color=_TITLE, anchor=MSO_ANCHOR.MIDDLE)
    _text(slide, meta["base_note"], 3.95, 1.18, 4.4, 0.38, size=11, color=_MUTED, anchor=MSO_ANCHOR.MIDDLE)
    add_rect(slide, 0.83, 1.66, 8.20, 0.012, _RULE)
    _place_element_list(slide, build, digits)

    image = _render(study, build)
    pack_h = 6.44
    pack_w = pack_h * study.aspect
    if image:
        slide.shapes.add_picture(str(image), Inches(9.20), Inches(0.46), Inches(pack_w), Inches(pack_h))

    places = "three decimals" if digits == 3 else "one decimal"
    _text(slide, _sample_line(study, group, kind, short=True), 0.75, 7.18, 4.6, 0.20, size=7, color=_MUTED)
    _text(
        slide,
        f"{study.model_name} model: element lifts show movement above or below the model baseline. Values rounded to {places}.",
        5.40, 7.18, 7.4, 0.20,
        size=7, bold=True, color=_TITLE,
    )
    return slide


def _place_element_list(slide, build: Build, digits: int = 1):
    """Grouped like the compact pack slide, with a small gap between rows.

    A list that would run past the sign splits into two columns.
    """
    elements = build.elements
    count = max(len(elements), 1)
    start = 1.78
    row_h = 0.46
    limit = 6.55
    fits = start + count * row_h + 0.50 <= limit

    if fits:
        _element_column(slide, elements, 0.83, 3.15, 4.05, 4.95, 4.05, start, row_h, digits)
        _total_row(slide, build.total, start + count * row_h + 0.06, 0.83, 3.15, 4.05, digits)
        return

    split = (count + 1) // 2
    columns = [elements[:split], elements[split:]]
    tallest = max(len(column) for column in columns)
    row_h = min(row_h, max(0.32, (limit - start - 0.50) / tallest))
    _element_column(slide, columns[0], 0.83, 1.85, 2.72, 3.50, 1.15, start, row_h, digits)
    if columns[1]:
        _element_column(slide, columns[1], 4.72, 1.85, 6.61, 7.39, 1.15, start, row_h, digits)
    _total_row(slide, build.total, start + tallest * row_h + 0.06, 0.83, 3.15, 4.05, digits)


def _element_column(slide, elements, name_x, name_w, pill_x, cat_x, cat_w, top, row_h, digits: int):
    for index, element in enumerate(elements):
        row_top = top + index * row_h
        name = _text(slide, element.label, name_x, row_top, name_w, row_h, size=10, bold=True, color=_INK, anchor=MSO_ANCHOR.MIDDLE)
        name.text_frame.word_wrap = False
        _score_pill(slide, element.lift, pill_x, row_top, row_h, size=12, digits=digits)
        _text(slide, element.category, cat_x, row_top, cat_w, row_h, size=10, color=_MUTED, anchor=MSO_ANCHOR.MIDDLE)


def _total_row(slide, total: float, top: float, label_x: float, label_w: float, pill_x: float, digits: int):
    add_rect(slide, label_x, top, label_w, 0.40, _BAR)
    _text(slide, "TOTAL LIFT OVER BASE", label_x, top, label_w, 0.40, size=9, bold=True, color=_TITLE, anchor=MSO_ANCHOR.MIDDLE)
    _score_pill(slide, total, pill_x, top, 0.40, size=14, digits=digits)


def _score_pill(slide, value: float, x: float, row_top: float, row_h: float, size: float, digits: int = 1):
    pill_h = 0.24 if row_h > 0.32 else 0.22
    pill_w = 0.96 if digits >= 3 else 0.78
    top = row_top + (row_h - pill_h) / 2
    cutoff = 0.0005 if digits >= 3 else 0.05
    fill = _PILL_UP if value > cutoff else _PILL_DOWN if value < -cutoff else _MUTED
    add_rect(slide, x, top, pill_w, pill_h, fill)
    _text(
        slide, _lift(value, digits),
        x, top, pill_w, pill_h,
        size=size, bold=True, color=WHITE, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE,
    )


def _variant_label(builds: list[Build], build: Build) -> str:
    if len(builds) < 2:
        return build.elements[0].label if build.elements else build.name
    shared = set(builds[0].elements[i].name for i in range(len(builds[0].elements)))
    for other in builds[1:]:
        names = {element.name for element in other.elements}
        shared &= names
    parts = [element.label for element in build.elements if element.name not in shared]
    return " · ".join(parts) if parts else (build.elements[0].label if build.elements else build.name)


def _render(study: StudyInfo, build: Build) -> Path | None:
    layers: list[Path] = []
    if build.background_path:
        layers.append(build.background_path)
    layers.extend(element.image_path for element in build.elements if element.image_path)
    if not layers:
        return None
    cache_root = layers[0].parent
    key = "-".join(re.sub(r"[^A-Za-z0-9]+", "_", part)[:40] for part in build.signature())
    return composite_pack(layers, cache_root.parent / "composites" / f"{key}.png", aspect=study.aspect)


def _sample_line(study: StudyInfo, group: Group, kind: str, short: bool = False) -> str:
    known = study.segment_sizes.get(group.segment)
    digits = _digits(kind)
    places = "three decimals" if digits == 3 else "one decimal"
    base_value = _base_for(study, kind)
    base = f" Base appeal {base_value:.{digits}f}." if base_value is not None else ""
    if short:
        if known:
            return f"n={known} {group.segment} respondents."
        return "Saved segment. Scores are from the overall model."
    if known:
        who = study.country if group.segment.lower() == "overall" else group.segment
        sample = f"n={known} {who} respondents."
    else:
        sample = f"Saved segment: {group.segment}. Scores are from the overall model."
    return (
        f"{sample}{base} "
        f"{study.model_name} model: estimated total combination lift above baseline. Values rounded to {places}."
    )


def _lift(value: float, digits: int = 1) -> str:
    return f"{value:+.{digits}f}"


def _tone(value: float) -> RGBColor:
    if value > 0.05:
        return GREEN
    if value < -0.05:
        return RED
    return GRAY
