"""Load an analysis export into the objects the report slides read."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Element:
    code: str
    name: str
    label: str
    lift: float
    significant: bool
    z_index: int = 0
    image_url: str | None = None
    image_path: Path | None = None


@dataclass
class Category:
    code: str
    name: str
    label: str
    elements: list[Element] = field(default_factory=list)

    @property
    def best(self) -> Element:
        return max(self.elements, key=lambda element: element.lift)

    @property
    def worst(self) -> Element:
        return min(self.elements, key=lambda element: element.lift)

    @property
    def spread(self) -> float:
        return self.best.lift - self.worst.lift


@dataclass
class Study:
    title: str
    launched_label: str
    language: str
    aspect_ratio: str
    study_id: str
    respondents: int
    responses: int
    avg_rating: float
    task_count: int
    base_appeal: float | None
    base_size: int
    model_name: str
    rating_bands: str
    categories: list[Category]
    rating_distribution: list[tuple[str, int]]
    age_distribution: list[tuple[str, int]]
    gender_distribution: list[tuple[str, int]]
    study_type: str = "layer"
    main_question: str = ""
    prepared_by: str = ""
    background_path: Path | None = None
    brand_logo: Path | None = None
    blocked: set[frozenset[str]] = field(default_factory=set)

    @property
    def element_count(self) -> int:
        return sum(len(category.elements) for category in self.categories)

    @property
    def silo_count(self) -> int:
        return len(self.categories)


def load_study(
    path: str | Path,
    cache_dir: str | Path | None = None,
    download_images: bool = True,
    study_type: str | None = None,
) -> Study:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    analysis = payload.get("analysis")
    if not isinstance(analysis, dict):
        raise ValueError("JSON is missing the analysis object.")
    overall = analysis.get("(T) Overall")
    intercepts = analysis.get("(T) Intercepts")
    if not isinstance(overall, dict) or not overall.get("categories"):
        raise ValueError("JSON is missing (T) Overall category scores. That is the Top-2 Box model this report charts.")
    if not isinstance(intercepts, dict):
        raise ValueError("JSON is missing (T) Intercepts.")
    raw_intercept = intercepts.get("intercept")
    base_appeal = float(raw_intercept) if raw_intercept is not None else None

    front = analysis.get("Front Page") or {}
    info = analysis.get("Information Block") or {}
    dashboard = analysis.get("dashboard_summary") or {}
    settings = analysis.get("analysis_settings") or {}

    kind = _resolve_study_type(study_type, payload, info)
    images = _artwork_index(info.get("Categories") or [])
    categories = []
    for raw in overall["categories"]:
        code = str(raw.get("code") or "")
        name = str(raw.get("name") or code)
        elements = []
        for item in raw.get("elements") or []:
            element_code = str(item.get("code") or "")
            element_name = str(item.get("name") or element_code)
            artwork = images.get(element_name) or images.get(element_code) or {}
            elements.append(
                Element(
                    code=element_code,
                    name=element_name,
                    label=element_name if kind == "text" else _pretty_element(element_name, element_code),
                    lift=float(item.get("value") or 0),
                    significant=bool(item.get("above_threshold")),
                    z_index=int(artwork.get("z") or 0),
                    image_url=artwork.get("url") if kind != "text" else None,
                )
            )
        if not elements:
            continue
        categories.append(
            Category(
                code=code,
                name=name,
                label=_pretty_category(name),
                elements=elements,
            )
        )
    if not categories:
        raise ValueError("No scored elements were found in (T) Overall.")

    title = str(front.get("Title") or info.get("Study Title") or "Design Element Appeal Report")
    launched = _format_launched(front.get("Launched At"))
    top_bands = ((settings.get("top") or {}).get("hundred")) or [4, 5]
    model_name, rating_bands = _model_label(top_bands)

    study = Study(
        title=title,
        launched_label=launched,
        language=str(front.get("Language") or "en"),
        aspect_ratio=str(front.get("Aspect Ratio") or info.get("Aspect Ratio") or ""),
        study_id=str(payload.get("study_id") or ""),
        respondents=int(dashboard.get("totalRespondents") or overall.get("base_size") or 0),
        responses=int(dashboard.get("totalResponses") or 0),
        avg_rating=float(dashboard.get("avgRating") or 0),
        task_count=int(dashboard.get("taskCount") or 0),
        base_appeal=base_appeal,
        base_size=int(overall.get("base_size") or dashboard.get("totalRespondents") or 0),
        model_name=model_name,
        rating_bands=rating_bands,
        categories=categories,
        rating_distribution=_pairs(dashboard.get("ratingDistribution")),
        age_distribution=_pairs(dashboard.get("ageDistribution")),
        gender_distribution=_pairs(dashboard.get("genderDistribution")),
        study_type=kind,
        main_question=str(
            front.get("Main Question")
            or info.get("Main Question")
            or payload.get("main_question")
            or ""
        ).strip(),
        prepared_by=str(front.get("Prepared By") or payload.get("prepared_by") or "").strip(),
        blocked=_load_constraints(source, categories),
    )

    if download_images:
        cache = Path(cache_dir) if cache_dir else source.parent / ".cache" / "elements"
        _download_assets(study, info.get("Study Background") or front.get("Background"), cache)
    return study


def cover_headline(study: Study) -> str:
    if study.main_question:
        return study.main_question
    if study.study_type == "text":
        return "Which statements drive appeal?"
    if study.study_type == "grid":
        return "Which images drive appeal?"
    title = study.title.lower()
    has_box = "box" in title
    has_aerosol = "aerosol" in title
    if has_box and not has_aerosol:
        return "Which box design elements drive consumer appeal?"
    if has_aerosol and not has_box:
        return "Which aerosol design elements drive consumer appeal?"
    if re.search(r"\bsign\b", title):
        return "Which sign design elements drive consumer appeal?"
    return "Which design elements drive consumer appeal?"


def strongest_silos(study: Study, limit: int = 4) -> list[Category]:
    ranked = sorted(study.categories, key=lambda category: (category.best.lift, category.spread), reverse=True)
    return ranked[:limit]


def category_story(category: Category) -> tuple[str, str, str]:
    best = category.best
    worst = category.worst
    label = category.label
    silo = label if any(ch.isdigit() for ch in label) else label.lower()
    if best.lift > 0 and worst.lift < 0:
        headline = f"{best.label} creates the clearest {silo} lift"
        detail = (
            f"{best.label} leads at { _signed(best.lift) }, while {worst.label} pulls appeal "
            f"the other way at { _signed(worst.lift) }."
        )
    elif best.lift > 0:
        headline = f"{best.label} leads the {silo} set"
        detail = (
            f"Every option is at or above the baseline. {best.label} is furthest ahead "
            f"at { _signed(best.lift) }."
        )
    else:
        headline = f"{label} sits below the baseline"
        detail = (
            f"No option in this silo lifts appeal. {best.label} is the least negative "
            f"at { _signed(best.lift) }; {worst.label} is the weakest at { _signed(worst.lift) }."
        )
    key = (
        f"Key read: {best.label} is the strongest {silo} direction "
        f"({ _signed(best.lift) }). The gap to {worst.label} is { _signed(category.spread) } points."
    )
    return headline, detail, key


def insight_cards(study: Study) -> list[tuple[str, str]]:
    elements = [element for category in study.categories for element in category.elements]
    strongest = max(elements, key=lambda element: element.lift)
    weakest = min(elements, key=lambda element: element.lift)
    strongest_category = max(study.categories, key=lambda category: category.best.lift)
    widest = max(study.categories, key=lambda category: category.spread)
    above = sum(1 for element in elements if element.lift > 0)
    strongest_home = next(category.label for category in study.categories if strongest in category.elements)
    weakest_home = next(category.label for category in study.categories if weakest in category.elements)
    if study.study_type == "text":
        return [
            (
                "The model has a clear starting point",
                (
                    f"Base appeal is {int(round(study.base_appeal))}%. That is the {study.model_name} baseline before any statement adds or subtracts lift."
                    if study.base_appeal is not None
                    else f"This {study.model_name} model was fit without an intercept. Each statement score is the lift on its own."
                ),
            ),
            (
                f"The strongest statement is in {strongest_home}",
                f"It moves appeal by { _signed(strongest.lift) }. The full wording is on the findings slides. {above} of {len(elements)} statements sit above the baseline.",
            ),
            (
                f"{strongest_category.label} is the lead group",
                f"Its strongest statement is { _signed(strongest_category.best.lift) }. {widest.label} has the widest spread, { _signed(widest.spread) } points.",
            ),
            (
                f"The weakest statement is in {weakest_home}",
                f"It sits at { _signed(weakest.lift) }. The full wording is on the findings slides. Negative lifts pull appeal below the baseline.",
            ),
        ]
    return [
        (
            "The model has a clear starting point",
            (
                f"Base appeal is {int(round(study.base_appeal))}%. That is the {study.model_name} baseline before any single design element adds or subtracts lift."
                if study.base_appeal is not None
                else f"This {study.model_name} model was fit without an intercept. Each element score is the lift on its own."
            ),
        ),
        (
            f"{strongest.label} is the strongest single cue",
            f"In {strongest_home}, {strongest.label} moves appeal by { _signed(strongest.lift) }. {above} of {len(elements)} elements sit above the baseline.",
        ),
        (
            f"{strongest_category.label} is the lead design area",
            f"{strongest_category.best.label} tops this silo at { _signed(strongest_category.best.lift) }. {widest.label} has the widest spread, { _signed(widest.spread) } points from {widest.worst.label} to {widest.best.label}.",
        ),
        (
            f"{weakest.label} is the largest drag",
            f"In {weakest_home}, {weakest.label} sits at { _signed(weakest.lift) }. Negative lifts pull the appeal score below the model baseline.",
        ),
    ]


def _signed(value: float) -> str:
    number = float(value)
    if abs(number - round(number)) < 0.05:
        number = int(round(number))
        return f"+{number}" if number > 0 else str(number)
    return f"{number:+.1f}"


def _pretty_category(name: str) -> str:
    cleaned = re.sub(r"^[A-Za-z]\.\s*", "", name).strip()
    return cleaned or name


def _pretty_element(name: str, code: str) -> str:
    text = name
    prefix = f"{code}-"
    if text.lower().startswith(prefix.lower()):
        text = text[len(prefix):]
    elif code and text.lower().startswith(code.lower()):
        text = text[len(code):].lstrip("-")
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\bsliver\b", "silver", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhollographic\b", "holographic", text, flags=re.IGNORECASE)
    text = re.sub(r"\brosegold\b", "rose gold", text, flags=re.IGNORECASE)
    text = re.sub(r"\b10xmais\b", "10x mais", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return name
    words = []
    for word in text.split(" "):
        token = word.lower()
        if token in {"100h", "10x", "16p"}:
            words.append(token.upper() if token != "16p" else "16p")
        elif any(character.isdigit() for character in word):
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _format_launched(value) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:7]
    return parsed.strftime("%B %Y")


def _model_label(bands) -> tuple[str, str]:
    try:
        numbers = [int(item) for item in bands]
    except (TypeError, ValueError):
        numbers = [4, 5]
    if not numbers:
        numbers = [4, 5]
    span = f"{min(numbers)}–{max(numbers)}" if numbers == list(range(min(numbers), max(numbers) + 1)) else ", ".join(str(n) for n in numbers)
    count = len(numbers)
    name = f"Top-{count} Box" if count > 1 else "Top Box"
    return name, f"ratings {span}"


def _pairs(rows) -> list[tuple[str, int]]:
    pairs = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("name") or "").strip()
        if label.lower().startswith("rating "):
            label = label.replace("Rating ", "Rating ")
        pairs.append((label[:1].upper() + label[1:] if label else "—", int(row.get("value") or 0)))
    return pairs


def _load_constraints(analysis_path: Path, categories: list[Category]) -> set[frozenset[str]]:
    """Pairs that cannot appear on the same design.

    study_data.json lists each rule as anchors and blocked images. If an anchor
    is on the pack, none of its blocked images may be on that pack. Fresh Harvest
    uses this so one message is not repeated as headline, sub-header, and CTA.
    """
    study_path = analysis_path.with_name("study_data.json")
    if not study_path.exists():
        return set()
    payload = json.loads(study_path.read_text(encoding="utf-8"))
    rules = payload.get("design_constraints") or []
    if not rules:
        return set()
    by_id: dict[str, str] = {}
    for layer in payload.get("layers") or []:
        for element in (layer.get("elements") or []) + (layer.get("images") or []):
            image_id = element.get("image_id") or element.get("id")
            name = element.get("name")
            if image_id and name:
                by_id[str(image_id)] = str(name)
    known = {element.name for category in categories for element in category.elements}
    blocked: set[frozenset[str]] = set()
    for rule in rules:
        anchors = [by_id.get(str(item.get("image_id"))) for item in rule.get("anchors") or []]
        others = [by_id.get(str(item.get("image_id"))) for item in rule.get("blocked") or []]
        for anchor in anchors:
            for other in others:
                if anchor and other and anchor != other and anchor in known and other in known:
                    blocked.add(frozenset((anchor, other)))
    return blocked


def legal_picks(study: Study, choose: str) -> list[Element]:
    """One element per silo. Highest or lowest total lift that keeps every blocked pair apart.

    With no pairing rules this is the best or worst element in each silo.
    On a tie, the element earlier in the (T) list stays.
    """
    categories = sorted(study.categories, key=lambda category: min(element.z_index for element in category.elements))
    if not study.blocked:
        return [category.best if choose == "best" else category.worst for category in categories]

    catalog = []
    for category in categories:
        ranked = sorted(
            enumerate(category.elements),
            key=lambda item: item[1].lift,
            reverse=choose == "best",
        )
        catalog.append(ranked)

    best: tuple | None = None
    winner: list[Element] = []

    def allowed(chosen: list[Element], candidate: Element) -> bool:
        return all(frozenset((previous.name, candidate.name)) not in study.blocked for previous in chosen)

    def search(index: int, chosen: list[Element], total: float, order: tuple[int, ...]) -> None:
        nonlocal best, winner
        if index == len(catalog):
            mark = (total, tuple(-step for step in order)) if choose == "best" else (-total, tuple(-step for step in order))
            if best is None or mark > best:
                best = mark
                winner = list(chosen)
            return
        if best is not None:
            if choose == "best":
                ceiling = total + sum(item[1].lift for item in (row[0] for row in catalog[index:]))
                if ceiling < best[0] - 1e-9:
                    return
            else:
                floor = total + sum(min(element.lift for _, element in row) for row in catalog[index:])
                if -floor < best[0] - 1e-9:
                    return
        for rank, element in catalog[index]:
            if not allowed(chosen, element):
                continue
            chosen.append(element)
            search(index + 1, chosen, total + element.lift, order + (rank,))
            chosen.pop()

    search(0, [], 0.0, ())
    if winner:
        return winner
    return [category.best if choose == "best" else category.worst for category in categories]


def _resolve_study_type(explicit: str | None, payload: dict, info: dict) -> str:
    """Layer, grid, or text. Hybrid keeps the layer layout until its own data arrives.

    An explicit value wins, then a study_type field on the JSON, then the
    study type stored in the analysis.
    """
    for candidate in (explicit, payload.get("study_type"), info.get("Study Type")):
        if not candidate:
            continue
        text = str(candidate).strip().lower()
        if text in {"layer", "grid", "text", "hybrid"}:
            return text
    return "layer"


def _artwork_index(categories) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for category in categories:
        for element in category.get("elements") or []:
            meta = {"url": _http_url(element.get("content")), "z": element.get("z_index") or 0}
            if element.get("name"):
                index[str(element["name"])] = meta
            if element.get("alt_text"):
                index.setdefault(str(element["alt_text"]), meta)
    return index


def _http_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    return None


_SOURCE_MAX_EDGE = 1600


def _shrink_source(data: bytes) -> bytes:
    """Downscale a source image so its long edge is <= ``_SOURCE_MAX_EDGE``.

    Original artwork is ~2480x3508. Every composite later resizes it down to
    <=1200px, so keeping the full-resolution source only makes each LANCZOS
    resize and encode slower. Shrinking once here (alpha preserved) makes all
    downstream work cheaper. Falls back to the raw bytes if anything fails.
    """
    try:
        import io as _io

        from PIL import Image as _Image

        with _Image.open(_io.BytesIO(data)) as img:
            img.load()
            long_edge = max(img.width, img.height)
            if long_edge <= _SOURCE_MAX_EDGE:
                return data
            scale = _SOURCE_MAX_EDGE / float(long_edge)
            new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            has_alpha = img.mode in ("RGBA", "LA", "P")
            resized = img.convert("RGBA" if has_alpha else "RGB").resize(
                new_size, _Image.Resampling.LANCZOS
            )
            out = _io.BytesIO()
            if has_alpha:
                resized.save(out, format="PNG", compress_level=1)
            else:
                resized.save(out, format="JPEG", quality=90)
            return out.getvalue()
    except Exception:
        return data


def _download_assets(study: Study, background_url: str | None, cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, Path, str]] = []
    background = _http_url(background_url)
    if background:
        dest = cache / "background.png"
        jobs.append(("background", dest, background))
    for category in study.categories:
        for element in category.elements:
            if not element.image_url:
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", element.name) or element.code
            dest = cache / f"{safe}.png"
            jobs.append((element.name, dest, element.image_url))

    saved: dict[str, Path] = {}

    # Cap per-image size and read time so one slow or oversized asset cannot
    # hold a worker long enough to trip an upstream gateway timeout. Writes go
    # to a unique temp file then os.replace, so the shared per-study cache is
    # safe when several exports run at once.
    _MAX_IMAGE_BYTES = 15 * 1024 * 1024

    def fetch(item: tuple[str, Path, str]):
        key, dest, url = item
        if dest.exists() and dest.stat().st_size > 0:
            return key, dest
        request = urllib.request.Request(
            quote(url, safe=":/?#[]@!$&'()*+,;=%"),
            headers={"User-Agent": "presentation-generator"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(_MAX_IMAGE_BYTES + 1)
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise ValueError(f"image too large or empty: {url[:120]}")
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
        try:
            payload = _shrink_source(data)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return key, dest

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, job) for job in jobs]
        for future in as_completed(futures):
            try:
                key, dest = future.result()
            except Exception as exc:
                print(f"  image skipped: {exc}")
                continue
            saved[key] = dest

    if "background" in saved:
        study.background_path = saved["background"]
    for category in study.categories:
        for element in category.elements:
            element.image_path = saved.get(element.name)
