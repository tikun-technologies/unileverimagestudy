"""Stack one element from each layer into a single pack image."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

# Source artwork is 2480×3508. Width / height.
PACK_ASPECT = 2480 / 3508


def _save_opaque(canvas: "Image.Image", destination: Path) -> None:
    """Write an opaque composite as JPEG.

    The previous PNG + ``optimize=True`` path was the single largest cost in a
    build (multi-pass zlib on every preview) and produced ~30 MB decks. These
    composites are always flattened onto a white background, so JPEG is visually
    equivalent for pack photography while encoding ~10x faster and cutting the
    file to a fraction of the size. python-pptx and PIL both detect the format
    from the bytes, so keeping the ``.png`` destination name is harmless.
    """
    rgb = canvas.convert("RGB")
    rgb.save(destination, format="JPEG", quality=88, optimize=False, progressive=False)


def composite_pack(layer_paths: list[Path], destination: Path, width: int = 900, aspect: float = PACK_ASPECT) -> Path | None:
    canvas = _stack(layer_paths, width, aspect)
    if canvas is None:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    _save_opaque(canvas, destination)
    return destination


def element_preview(
    background: Path | None,
    element_path: Path,
    destination: Path,
    width: int = 900,
    tight: bool = False,
    frame: tuple[float, float, float, float] | None = None,
    full: bool = False,
) -> Path | None:
    """Background stays underneath the element.

    ``full`` keeps the whole pack, so the study background is not cropped away.
    ``frame`` is a shared window as fractions of the pack. Every element in a
    silo uses the same window, so a mark that only moves up or down stays put
    in the picture instead of being recentered.
    """
    layers = [path for path in (background, element_path) if path and path.exists()]
    if not layers:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    stamp = max(path.stat().st_mtime for path in layers)
    if destination.exists() and destination.stat().st_size > 0 and destination.stat().st_mtime >= stamp:
        return destination
    canvas = _stack(layers, width)
    if canvas is None:
        return None
    if full:
        pass
    elif frame is not None:
        canvas = _crop_fraction(canvas, frame)
    elif element_path and element_path.exists():
        element = Image.open(element_path).convert("RGBA")
        if element.size != canvas.size:
            element = element.resize(canvas.size, Image.Resampling.LANCZOS)
        canvas = _focus_crop(canvas, element, tight=tight)
    _save_opaque(canvas, destination)
    return destination


def gallery_frame(element_paths: list[Path]) -> tuple[float, float, float, float] | None:
    """One crop window for a silo whose options are the same mark in different places.

    Returns fractions ``(left, top, right, bottom)``. ``None`` means each
    element should be cropped on its own, because the artworks differ in shape.
    """
    paths = [path for path in element_paths if path and path.exists()]
    if not paths:
        return None
    with Image.open(paths[0]) as sample:
        width, height = sample.size
    boxes: list[tuple[int, int, int, int]] = []
    for path in paths:
        with Image.open(path) as image:
            alpha = image.convert("RGBA")
            if alpha.size != (width, height):
                alpha = alpha.resize((width, height), Image.Resampling.LANCZOS)
            box = alpha.getchannel("A").getbbox()
        if box:
            boxes.append(box)
    if not boxes:
        return (0.12, 0.10, 0.88, 0.90)
    full_face = [
        box for box in boxes
        if (box[2] - box[0]) > width * 0.8 and (box[3] - box[1]) > height * 0.55
    ]
    if full_face and len(full_face) == len(boxes):
        return _window_fractions(_union(full_face), width, height, pad_x=width * 0.012, pad_y=height * 0.012)
    areas = [(box[2] - box[0]) * (box[3] - box[1]) for box in boxes]
    ink_h = max(box[3] - box[1] for box in boxes)
    ink_w = max(box[2] - box[0] for box in boxes)
    union = _union(boxes)
    union_h = union[3] - union[1]
    if max(areas) > min(areas) * 3.2 or ink_h / max(union_h, 1) < 0.08:
        return None
    crop_h = max(union_h + ink_h * 0.9, ink_h / 0.15)
    crop_w = max((union[2] - union[0]) + ink_w * 0.45, (union[2] - union[0]) * 1.35)
    if crop_h and crop_w / crop_h > 1.7:
        crop_h = crop_w / 1.55
    elif crop_h and crop_w / crop_h < 0.8:
        crop_w = crop_h * 0.9
    cx = (union[0] + union[2]) / 2
    cy = (union[1] + union[3]) / 2
    left = cx - crop_w / 2
    top = cy - crop_h / 2
    right = left + crop_w
    bottom = top + crop_h
    if left < 0:
        right -= left
        left = 0
    if top < 0:
        bottom -= top
        top = 0
    if right > width:
        left -= right - width
        right = width
    if bottom > height:
        top -= bottom - height
        bottom = height
    return _window_fractions((max(0, left), max(0, top), right, bottom), width, height, pad_x=0, pad_y=0)


def _stack(layer_paths: list[Path], width: int, aspect: float = PACK_ASPECT) -> Image.Image | None:
    paths = [path for path in layer_paths if path and path.exists()]
    if not paths:
        return None
    height = max(1, round(width / aspect))
    size = (width, height)
    canvas = Image.new("RGBA", size, (255, 255, 255, 255))
    for path in paths:
        layer = Image.open(path).convert("RGBA")
        if layer.size != size:
            layer = layer.resize(size, Image.Resampling.LANCZOS)
        canvas = Image.alpha_composite(canvas, layer)
    return canvas


def _union(boxes: list[tuple]) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _window_fractions(box, width: float, height: float, pad_x: float, pad_y: float) -> tuple[float, float, float, float]:
    left = max(0.0, box[0] - pad_x)
    top = max(0.0, box[1] - pad_y)
    right = min(float(width), box[2] + pad_x)
    bottom = min(float(height), box[3] + pad_y)
    if right <= left or bottom <= top:
        return (0.0, 0.0, 1.0, 1.0)
    return (left / width, top / height, right / width, bottom / height)


def _crop_fraction(canvas: Image.Image, frame: tuple[float, float, float, float]) -> Image.Image:
    width, height = canvas.size
    left, top, right, bottom = frame
    box = (
        max(0, min(width - 1, int(left * width))),
        max(0, min(height - 1, int(top * height))),
        max(1, min(width, int(right * width))),
        max(1, min(height, int(bottom * height))),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return canvas
    return canvas.crop(box)


def _focus_crop(canvas: Image.Image, element: Image.Image, tight: bool = False) -> Image.Image:
    bbox = element.getchannel("A").getbbox()
    if bbox is None:
        return canvas
    width, height = canvas.size
    x0, y0, x1, y1 = bbox
    box_w, box_h = x1 - x0, y1 - y0
    # A full box face is the whole pack. Everything else zooms toward the layer.
    if box_w > width * 0.8 and box_h > height * 0.55:
        return canvas
    center_x = (x0 + x1) / 2
    center_y = (y0 + y1) / 2
    if tight:
        # Pad the ink. Do not stretch the frame, or a neighbouring logo gets sliced.
        pad_x = max(box_w * 0.18, width * 0.008)
        pad_y = max(box_h * 0.06, height * 0.008)
        crop_w = box_w + pad_x * 2
        crop_h = box_h + pad_y * 2
    else:
        crop_w = max(box_w + width * 0.1, width * 0.38)
        crop_h = max(box_h + height * 0.08, height * 0.28)
    left = int(center_x - crop_w / 2)
    top = int(center_y - crop_h / 2)
    right = int(left + crop_w)
    bottom = int(top + crop_h)
    if left < 0:
        right -= left
        left = 0
    if top < 0:
        bottom -= top
        top = 0
    if right > width:
        left -= right - width
        right = width
    if bottom > height:
        top -= bottom - height
        bottom = height
    left = max(0, left)
    top = max(0, top)
    return canvas.crop((left, top, right, bottom))
