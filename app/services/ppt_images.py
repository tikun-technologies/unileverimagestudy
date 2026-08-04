"""Download and compose study visuals for PowerPoint export.

Mirrors the design configurator / canvas-export stacking rules:
- layer studies: background object-contain fit box, layers sorted by z_index,
  transforms as percent of the fit box, each layer drawn with object-contain
- grid / hybrid: thumbnail mosaic of element images
- text: stacked name cards
"""

from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_TIMEOUT = 12.0
_MAX_BYTES = 8 * 1024 * 1024


class ImageCache:
    """Per-export URL → PIL image cache."""

    def __init__(self) -> None:
        self._cache: Dict[str, Optional[Image.Image]] = {}

    def get(self, url: Optional[str]) -> Optional[Image.Image]:
        if not url or not str(url).startswith(("http://", "https://")):
            return None
        key = str(url).strip()
        if key in self._cache:
            cached = self._cache[key]
            return cached.copy() if cached is not None else None
        image = _download_image(key)
        self._cache[key] = image
        return image.copy() if image is not None else None


def _download_image(url: str) -> Optional[Image.Image]:
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "MindSurve-PPT/1.0"})
            response.raise_for_status()
            if len(response.content) > _MAX_BYTES:
                logger.warning("Image too large, skipping: %s", url[:120])
                return None
            image = Image.open(io.BytesIO(response.content))
            image.load()
            return image.convert("RGBA")
    except Exception as exc:
        logger.info("Failed to download image for PPT: %s (%s)", url[:120], exc)
        return None


def _parse_aspect(aspect_ratio: Optional[str]) -> Tuple[int, int]:
    raw = (aspect_ratio or "9 / 16").replace(" ", "").replace(":", "/")
    if raw in {"16/9"}:
        return 1920, 1080
    if raw in {"1/1"}:
        return 1080, 1080
    return 1080, 1920


def _contain_rect(
    img_w: int, img_h: int, box_x: float, box_y: float, box_w: float, box_h: float
) -> Tuple[float, float, float, float]:
    if img_w <= 0 or img_h <= 0 or box_w <= 0 or box_h <= 0:
        return box_x, box_y, box_w, box_h
    img_aspect = img_w / img_h
    box_aspect = box_w / box_h
    if img_aspect > box_aspect:
        draw_w = box_w
        draw_h = box_w / img_aspect
    else:
        draw_h = box_h
        draw_w = box_h * img_aspect
    draw_x = box_x + (box_w - draw_w) / 2
    draw_y = box_y + (box_h - draw_h) / 2
    return draw_x, draw_y, draw_w, draw_h


def _paste_contain(
    canvas: Image.Image,
    img: Image.Image,
    box_x: float,
    box_y: float,
    box_w: float,
    box_h: float,
) -> None:
    dx, dy, dw, dh = _contain_rect(img.width, img.height, box_x, box_y, box_w, box_h)
    if dw < 1 or dh < 1:
        return
    resized = img.resize((max(1, int(dw)), max(1, int(dh))), Image.Resampling.LANCZOS)
    canvas.alpha_composite(resized, (int(dx), int(dy)))


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "Arial.ttf", "calibri.ttf", "Calibri.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_text_card(
    canvas: Image.Image,
    text: str,
    box_x: float,
    box_y: float,
    box_w: float,
    box_h: float,
) -> None:
    draw = ImageDraw.Draw(canvas)
    x0, y0 = int(box_x), int(box_y)
    x1, y1 = int(box_x + box_w), int(box_y + box_h)
    pad = max(4, int(min(box_w, box_h) * 0.04))
    draw.rounded_rectangle(
        [x0 + pad, y0 + pad, x1 - pad, y1 - pad],
        radius=max(6, pad),
        fill=(255, 255, 255, 230),
        outline=(226, 232, 240, 255),
        width=2,
    )
    label = " ".join(str(text or "").split())[:80] or "—"
    font = _font(max(14, int(min(box_w, box_h) * 0.08)))
    # Simple centered multiline wrap
    max_chars = max(8, int(box_w / max(font.size * 0.55, 1)))
    words = label.split()
    lines: List[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if len(trial) <= max_chars:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    lines = lines[:4]
    line_h = font.size + 4
    total_h = line_h * len(lines)
    ty = y0 + (box_h - total_h) / 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        tx = x0 + (box_w - tw) / 2
        draw.text((tx, ty), line, fill=(17, 24, 39, 255), font=font)
        ty += line_h


def _is_text_element(element: Dict[str, Any]) -> bool:
    if not element.get("image_url"):
        return True
    return str(element.get("element_type") or "").casefold() == "text"


def _sorted_elements(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        list(elements or []),
        key=lambda el: (
            int(el.get("z_index") or 0),
            str(el.get("category_name") or el.get("category") or "").casefold(),
            str(el.get("name") or "").casefold(),
        ),
    )


def compose_layer_design(
    elements: List[Dict[str, Any]],
    *,
    background_url: Optional[str],
    aspect_ratio: Optional[str],
    cache: ImageCache,
) -> bytes:
    """Compose a layer design PNG matching configurator z-index stacking."""
    canvas_w, canvas_h = _parse_aspect(aspect_ratio)
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (248, 250, 252, 255))
    bg_rect = {"x": 0.0, "y": 0.0, "width": float(canvas_w), "height": float(canvas_h)}

    bg = cache.get(background_url)
    if bg is not None:
        img_aspect = bg.width / max(bg.height, 1)
        target_aspect = canvas_w / canvas_h
        if img_aspect > target_aspect:
            bg_rect["width"] = float(canvas_w)
            bg_rect["height"] = canvas_w / img_aspect
            bg_rect["y"] = (canvas_h - bg_rect["height"]) / 2
        else:
            bg_rect["height"] = float(canvas_h)
            bg_rect["width"] = canvas_h * img_aspect
            bg_rect["x"] = (canvas_w - bg_rect["width"]) / 2
        resized = bg.resize(
            (max(1, int(bg_rect["width"])), max(1, int(bg_rect["height"]))),
            Image.Resampling.LANCZOS,
        )
        canvas.alpha_composite(resized, (int(bg_rect["x"]), int(bg_rect["y"])))

    for element in _sorted_elements(elements):
        transform = element.get("transform") or {}
        try:
            width_pct = max(1.0, min(100.0, float(transform.get("width", 100))))
        except (TypeError, ValueError):
            width_pct = 100.0
        try:
            height_pct = max(1.0, min(100.0, float(transform.get("height", 100))))
        except (TypeError, ValueError):
            height_pct = 100.0
        try:
            x_pct = max(0.0, min(100.0 - width_pct, float(transform.get("x", 0))))
        except (TypeError, ValueError):
            x_pct = 0.0
        try:
            y_pct = max(0.0, min(100.0 - height_pct, float(transform.get("y", 0))))
        except (TypeError, ValueError):
            y_pct = 0.0

        px_w = (width_pct / 100.0) * bg_rect["width"]
        px_h = (height_pct / 100.0) * bg_rect["height"]
        px_x = bg_rect["x"] + (x_pct / 100.0) * bg_rect["width"]
        px_y = bg_rect["y"] + (y_pct / 100.0) * bg_rect["height"]

        if _is_text_element(element):
            _draw_text_card(canvas, str(element.get("name") or ""), px_x, px_y, px_w, px_h)
            continue
        layer_img = cache.get(element.get("image_url"))
        if layer_img is None:
            _draw_text_card(canvas, str(element.get("name") or ""), px_x, px_y, px_w, px_h)
            continue
        _paste_contain(canvas, layer_img, px_x, px_y, px_w, px_h)

    return _to_png_bytes(canvas)


def compose_mosaic_design(
    elements: List[Dict[str, Any]],
    *,
    cache: ImageCache,
    size: int = 720,
) -> bytes:
    """Grid / hybrid / text mosaic matching DesignPreviewComposite."""
    items = _sorted_elements(elements)
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=24, fill=(255, 255, 255, 255), outline=(229, 231, 235, 255))

    count = max(1, len(items))
    cols = 1 if count == 1 else (2 if count == 2 or count >= 4 else 3)
    rows = (count + cols - 1) // cols
    pad = 16
    gap = 12
    cell_w = (size - pad * 2 - gap * (cols - 1)) / cols
    cell_h = (size - pad * 2 - gap * (rows - 1)) / rows

    for idx, element in enumerate(items[: cols * rows]):
        row, col = divmod(idx, cols)
        x = pad + col * (cell_w + gap)
        y = pad + row * (cell_h + gap)
        draw.rounded_rectangle(
            [x, y, x + cell_w, y + cell_h],
            radius=12,
            fill=(248, 250, 252, 255),
            outline=(243, 244, 246, 255),
        )
        if _is_text_element(element):
            _draw_text_card(canvas, str(element.get("name") or ""), x, y, cell_w, cell_h)
            continue
        img = cache.get(element.get("image_url"))
        if img is None:
            _draw_text_card(canvas, str(element.get("name") or ""), x, y, cell_w, cell_h)
        else:
            inner = 8
            _paste_contain(canvas, img, x + inner, y + inner, cell_w - inner * 2, cell_h - inner * 2)

    return _to_png_bytes(canvas)


def compose_design_preview(
    design: Dict[str, Any],
    *,
    study_type: str,
    background_url: Optional[str],
    aspect_ratio: Optional[str],
    cache: ImageCache,
) -> bytes:
    elements = list(design.get("elements") or [])
    if (study_type or "").casefold() == "layer":
        return compose_layer_design(
            elements,
            background_url=background_url,
            aspect_ratio=aspect_ratio,
            cache=cache,
        )
    return compose_mosaic_design(elements, cache=cache)


def compose_element_thumb(
    element: Dict[str, Any],
    *,
    cache: ImageCache,
    size: int = 360,
) -> bytes:
    canvas = Image.new("RGBA", (size, size), (248, 250, 252, 255))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=20, fill=(248, 250, 252, 255), outline=(229, 231, 235, 255))
    if _is_text_element(element):
        _draw_text_card(canvas, str(element.get("name") or ""), 8, 8, size - 16, size - 16)
        return _to_png_bytes(canvas)
    img = cache.get(element.get("image_url"))
    if img is None:
        _draw_text_card(canvas, str(element.get("name") or ""), 8, 8, size - 16, size - 16)
    else:
        _paste_contain(canvas, img, 16, 16, size - 32, size - 32)
    return _to_png_bytes(canvas)


def _to_png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
