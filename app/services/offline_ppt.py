"""Package the live report code/artwork for execution inside an offline HTML.

No presentation is rendered here. The browser runs these exact source files;
this avoids maintaining a second slide layout implementation.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import hashlib
from pathlib import Path


@lru_cache(maxsize=1)
def report_files():
    root = Path(__file__).parent / "reportgen"
    paths = [root / name for name in (
        "__init__.py", "build.py", "charts.py", "combinations.py", "composite.py",
        "default_report.py", "study.py", "theme.py",
    )]
    paths += sorted((root / "fonts").glob("*.ttf"))
    paths += sorted((root / "template_assets").glob("*.png"))
    files = {str(path.relative_to(root)).replace("\\", "/"): base64.b64encode(path.read_bytes()).decode("ascii") for path in paths}
    digest = hashlib.sha256("".join(f"{name}:{data}" for name, data in sorted(files.items())).encode()).hexdigest()
    return {"format": "reportgen-v1", "sha256": digest, "files": files}


def offline_report_source(study, study_data, exporter):
    from app.services.appeal_report import _constraint_study_data, _image_cache_dir, _prepared_by_credit
    from app.services.reportgen.study import fetch_image

    # Use the same cached, resized source bytes as the live deck. The HTML's
    # smaller lossy previews would otherwise change composites and image crops.
    urls = {item.get("content") for item in study_data.get("elements", [])}
    urls.add(study_data.get("background"))
    urls = sorted(url for url in urls if isinstance(url, str) and url.startswith(("http://", "https://"))
                  and not url.split("?")[0].lower().endswith((".mp4", ".m4v", ".mov", ".webm", ".m3u8")))
    cache = _image_cache_dir(study.id)

    def read_artwork(url):
        destination = cache / f"offline-{hashlib.sha256(url.encode()).hexdigest()[:24]}.png"
        try:
            fetch_image(destination, url)
            data = destination.read_bytes()
            mime = "image/jpeg" if data.startswith(b"\xff\xd8") else "image/png"
            return url, f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        except Exception:
            return url, None  # The live generator also skips unavailable artwork.

    with ThreadPoolExecutor(max_workers=8) as pool:
        artwork = dict(pool.map(read_artwork, urls))
    return {
        **report_files(),
        "artwork": artwork,
        "mainQuestion": " ".join(str(getattr(study, "main_question", None) or "").split()),
        "preparedBy": _prepared_by_credit(getattr(exporter, "name", None), getattr(exporter, "email", None)),
        "constraintStudy": _constraint_study_data(study),
    }
