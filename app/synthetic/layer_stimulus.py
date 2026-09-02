"""
Compose layer-study stimuli the same way /participate renders them:
background (object-contain) + layers stacked by z_index with percent transforms.

Used by synthetic AI so the model rates one assembled design, not loose assets.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from app.services.ppt_images import ImageCache, compose_layer_design

logger = logging.getLogger(__name__)

DEFAULT_TRANSFORM: Dict[str, float] = {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff")
NON_IMAGE_EXTENSIONS = (".txt", ".html", ".htm", ".json", ".csv", ".xml", ".pdf")
AZURE_BLOB_HOST_MARKERS = (
    ".blob.core.windows.net",
    ".blob.core.chinacloudapi.cn",
    ".blob.core.usgovcloudapi.net",
    ".blob.core.cloudapi.de",
)
MAX_VISION_SIDE = 1280
MAX_JPEG_BYTES = 4_500_000
COMPOSITE_CACHE_SIZE = 80


def normalize_study_type(value: Any) -> str:
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    text = str(raw).strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def is_layer_study(study_data: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(study_data, dict):
        return False
    if normalize_study_type(study_data.get("study_type")) == "layer":
        return True
    layout = study_data.get("layer_layout")
    return isinstance(layout, dict) and len(layout) > 0


def is_shown_flag(value: Any) -> bool:
    if value is True:
        return True
    if value in (1, 1.0, "1"):
        return True
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return False


def normalize_transform(raw: Any) -> Dict[str, float]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = None
    if not isinstance(raw, dict):
        return dict(DEFAULT_TRANSFORM)

    def _pct(key: str, default: float, lo: float, hi: float, *, zero_means_default: bool = False) -> float:
        try:
            num = float(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        # Match participate: Number(t.width) || 100 treats 0 as missing.
        if zero_means_default and num == 0:
            return default
        return max(lo, min(hi, num))

    width = _pct("width", 100.0, 1.0, 100.0, zero_means_default=True)
    height = _pct("height", 100.0, 1.0, 100.0, zero_means_default=True)
    x = _pct("x", 0.0, 0.0, max(0.0, 100.0 - width))
    y = _pct("y", 0.0, 0.0, max(0.0, 100.0 - height))
    return {"x": x, "y": y, "width": width, "height": height}


def normalize_z_index(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def resolve_aspect_ratio(study_data: Optional[Dict[str, Any]]) -> str:
    if not isinstance(study_data, dict):
        return "9 / 16"
    seg = study_data.get("audience_segmentation")
    from_seg = seg.get("aspect_ratio") if isinstance(seg, dict) else None
    raw = study_data.get("aspect_ratio") or from_seg or "9 / 16"
    text = str(raw).strip()
    return text or "9 / 16"


def task_content_map(task: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(task, dict):
        return {}
    content = task.get("elements_shown_content")
    if not isinstance(content, dict) or not content:
        content = task.get("layers_shown_in_task")
    return content if isinstance(content, dict) else {}


def iter_shown_elements(task: Optional[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    if not isinstance(task, dict):
        return []
    shown = task.get("elements_shown")
    if not isinstance(shown, dict):
        shown = {}
    content = task_content_map(task)
    if shown:
        keys = [key for key, value in shown.items() if is_shown_flag(value)]
    else:
        keys = [
            key
            for key, value in content.items()
            if isinstance(value, dict) and (value.get("url") or value.get("content") or value.get("image_url"))
        ]
    out: List[Tuple[str, Dict[str, Any]]] = []
    for key in keys:
        data = content.get(key)
        if isinstance(data, dict) and data:
            out.append((str(key), data))
    return out


def resolve_layer_name(key: str, element: Dict[str, Any]) -> str:
    for candidate in (element.get("layer_name"), element.get("category_name")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    if isinstance(key, str) and "_" in key:
        return key.rsplit("_", 1)[0].strip() or "Unknown"
    return (str(key or "").strip() or "Unknown")


def lookup_layer_layout(study_data: Optional[Dict[str, Any]], layer_name: str) -> Dict[str, Any]:
    if not isinstance(study_data, dict):
        return {}
    layout = study_data.get("layer_layout")
    if not isinstance(layout, dict):
        return {}
    if layer_name in layout and isinstance(layout[layer_name], dict):
        return layout[layer_name]
    want = str(layer_name or "").casefold()
    if not want:
        return {}
    for key, value in layout.items():
        if str(key).casefold() == want and isinstance(value, dict):
            return value
    return {}


def element_image_url(element: Dict[str, Any]) -> Optional[str]:
    for key in ("url", "image_url", "content"):
        value = element.get(key)
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return value.strip()
    return None


def _is_azure_blob_url(url: str) -> bool:
    try:
        host = url.split("://", 1)[1].split("/", 1)[0].lower()
    except IndexError:
        return False
    return any(host.endswith(marker) for marker in AZURE_BLOB_HOST_MARKERS)


def is_probably_image_url(url: Optional[str], element_type: Optional[str] = None, *, layer_mode: bool = False) -> bool:
    """Treat Azure Blob URLs as images even with no file extension and a SAS query."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    path = url.split("?", 1)[0].lower()
    if any(path.endswith(ext) for ext in NON_IMAGE_EXTENSIONS):
        return False
    type_name = str(element_type or "").strip().lower()
    if type_name == "image":
        return True
    if any(path.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return True
    if _is_azure_blob_url(url):
        return type_name != "text"
    if type_name == "text":
        return False
    return layer_mode


def enrich_shown_element(
    key: str,
    element: Dict[str, Any],
    study_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    layer_name = resolve_layer_name(key, element)
    layout = lookup_layer_layout(study_data, layer_name)
    transform = normalize_transform(element.get("transform") if element.get("transform") is not None else layout.get("transform"))
    z_raw = element.get("z_index")
    if z_raw is None:
        z_raw = layout.get("z_index")
    url = element_image_url(element)
    element_type = element.get("element_type") or layout.get("layer_type") or ("image" if url else "text")
    return {
        "key": key,
        "element_id": element.get("element_id"),
        "name": element.get("name") or layer_name,
        "content": url or element.get("content") or element.get("name"),
        "url": url,
        "category_name": layer_name,
        "layer_name": layer_name,
        "element_type": str(element_type),
        "z_index": normalize_z_index(z_raw),
        "transform": transform,
    }


def build_compose_elements(task: Dict[str, Any], study_data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    elements: List[Dict[str, Any]] = []
    for key, raw in iter_shown_elements(task):
        enriched = enrich_shown_element(key, raw, study_data)
        url = enriched.get("url")
        if not url:
            continue
        elements.append({
            "name": enriched.get("name") or enriched.get("layer_name"),
            "image_url": url,
            "z_index": enriched.get("z_index", 0),
            "transform": enriched.get("transform") or dict(DEFAULT_TRANSFORM),
            "element_type": "image",
            "category_name": enriched.get("layer_name"),
        })
    return elements


def describe_layer_stack(task: Dict[str, Any], study_data: Optional[Dict[str, Any]]) -> str:
    bg = ""
    if isinstance(study_data, dict):
        bg = str(study_data.get("background_image_url") or "").strip()
    lines = [
        "Composed design (same stack shown to participants):",
        f"- Background image: {'yes' if bg else 'none'}",
    ]
    for key, raw in iter_shown_elements(task):
        enriched = enrich_shown_element(key, raw, study_data)
        tf = enriched["transform"]
        lines.append(
            f"- Layer \"{enriched['layer_name']}\" "
            f"(z-index={enriched['z_index']}, "
            f"x={tf['x']:g}%, y={tf['y']:g}%, "
            f"width={tf['width']:g}%, height={tf['height']:g}%)"
        )
    return "\n".join(lines)


def encode_composed_image_for_vision(png_bytes: bytes) -> str:
    image = Image.open(io.BytesIO(png_bytes))
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (248, 250, 252))
        canvas.paste(rgba, mask=rgba.split()[-1])
        image = canvas
    else:
        image = image.convert("RGB")

    width, height = image.size
    longest = max(width, height)
    if longest > MAX_VISION_SIDE:
        scale = MAX_VISION_SIDE / float(longest)
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )

    jpeg_bytes = b""
    for quality in (85, 75, 65, 55):
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality, optimize=True)
        jpeg_bytes = buf.getvalue()
        if len(jpeg_bytes) <= MAX_JPEG_BYTES:
            break
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _fingerprint(background_url: Optional[str], aspect: str, elements: List[Dict[str, Any]]) -> str:
    payload = {
        "bg": background_url or "",
        "aspect": aspect or "",
        "els": [
            {
                "u": item.get("image_url"),
                "z": item.get("z_index"),
                "t": item.get("transform"),
            }
            for item in elements
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class StimulusComposer:
    """Download/cache layer assets and compose one JPEG data URL per unique stack."""

    def __init__(self, image_cache: Optional[ImageCache] = None) -> None:
        self.cache = image_cache or ImageCache()
        self._jpeg_lru: OrderedDict[str, str] = OrderedDict()
        self._jpeg_lock = threading.Lock()
        self._max_jpeg = COMPOSITE_CACHE_SIZE

    def compose_data_url(self, task: Dict[str, Any], study_data: Optional[Dict[str, Any]]) -> Optional[str]:
        try:
            elements = build_compose_elements(task, study_data)
            if not elements:
                return None
            background_url = None
            if isinstance(study_data, dict):
                raw_bg = study_data.get("background_image_url")
                if isinstance(raw_bg, str) and raw_bg.strip().startswith(("http://", "https://")):
                    background_url = raw_bg.strip()
            aspect = resolve_aspect_ratio(study_data)
            fingerprint = _fingerprint(background_url, aspect, elements)

            with self._jpeg_lock:
                cached = self._jpeg_lru.get(fingerprint)
                if cached:
                    self._jpeg_lru.move_to_end(fingerprint)
                    return cached

            has_pixels = bool(background_url and self.cache.get(background_url) is not None)
            if not has_pixels:
                for item in elements:
                    url = item.get("image_url")
                    if url and self.cache.get(url) is not None:
                        has_pixels = True
                        break
            if not has_pixels:
                logger.warning("Layer compose skipped: no downloadable background or layer images")
                return None

            png_bytes = compose_layer_design(
                elements,
                background_url=background_url,
                aspect_ratio=aspect,
                cache=self.cache,
            )
            if not png_bytes:
                return None
            data_url = encode_composed_image_for_vision(png_bytes)
            with self._jpeg_lock:
                self._jpeg_lru[fingerprint] = data_url
                self._jpeg_lru.move_to_end(fingerprint)
                while len(self._jpeg_lru) > self._max_jpeg:
                    self._jpeg_lru.popitem(last=False)
            return data_url
        except Exception:
            logger.exception("Failed to compose layer stimulus for synthetic AI")
            return None
