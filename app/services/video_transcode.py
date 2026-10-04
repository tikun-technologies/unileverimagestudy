"""Download a raw video, write 360p/720p HLS plus a poster, upload to Blob."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from azure.storage.blob import BlobServiceClient, ContentSettings

# The Azure SDK logs full request/response headers per blob operation at INFO,
# which is hundreds of lines per encode. Keep only warnings and errors.
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure.storage").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    extra = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin" / f"{name}.exe",
        Path(r"C:\ffmpeg\bin") / f"{name}.exe",
    ]
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if packages.is_dir():
        extra.extend(packages.glob(f"**/bin/{name}.exe"))
    for candidate in extra:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError(
        f"{name} is not installed or not on PATH. "
        "Install FFmpeg (winget install Gyan.FFmpeg), close this terminal, and start the video worker again."
    )


def _run(cmd: list[str]) -> None:
    logger.info("ffmpeg %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _has_audio(path: Path) -> bool:
    result = subprocess.run(
        [
            _tool("ffprobe"), "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
    )
    return bool((result.stdout or "").strip())


def _blob_name_from_url(source_url: str, container: str) -> str | None:
    """Blob path for either a storage URL or a CDN URL of the same container."""
    marker = f"/{container}/"
    if marker not in source_url:
        return None
    name = source_url.split(marker, 1)[1].split("?", 1)[0]
    name = urllib.parse.unquote(name).strip("/")
    return name or None


def _download(source_url: str, dest: Path, container: str, connection: str) -> None:
    blob_name = _blob_name_from_url(source_url, container) if connection else None
    if blob_name:
        client = BlobServiceClient.from_connection_string(connection)
        blob = client.get_blob_client(container=container, blob=blob_name)
        with dest.open("wb") as handle:
            blob.download_blob().readinto(handle)
        return
    request = urllib.request.Request(source_url)
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _rendition(src: Path, height: int, bitrate: str, out_dir: Path, with_audio: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
        "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate,
        # 2s GOP so HLS segments are 2s: the first segment downloads fast,
        # which makes playback start quicker on mobile networks.
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
    ]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [
        "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
        str(out_dir / "index.m3u8"),
    ]
    _run(cmd)


def _poster(src: Path, dest: Path) -> None:
    cmd = [
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.5", "-i", str(src),
        "-frames:v", "1", "-update", "1", "-vf", "scale=-2:720", str(dest),
    ]
    try:
        _run(cmd)
    except subprocess.CalledProcessError:
        cmd[cmd.index("0.5")] = "0"
        _run(cmd)


def _master(out_dir: Path) -> None:
    playlist = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=600000,RESOLUTION=640x360
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1600000,RESOLUTION=1280x720
720/index.m3u8
"""
    (out_dir / "master.m3u8").write_text(playlist, encoding="utf-8")


def _upload_one(client: BlobServiceClient, container: str, prefix: str, path: Path, out_dir: Path) -> None:
    relative = path.relative_to(out_dir).as_posix()
    blob_name = f"{prefix.rstrip('/')}/{relative}"
    content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    blob = client.get_blob_client(container=container, blob=blob_name)
    with path.open("rb") as handle:
        blob.upload_blob(
            handle,
            overwrite=True,
            # HLS output paths use a fresh, UUID-based prefix per encode, so
            # playlists, segments, and posters are immutable and safe to keep
            # in the browser cache. This lets prewarmed segments be reused by
            # hls.js when the participant scrolls to the clip.
            content_settings=ContentSettings(
                content_type=content_type,
                cache_control="public, max-age=31536000, immutable",
            ),
        )


def _upload_tree(out_dir: Path, container: str, prefix: str, connection: str) -> int:
    files = [path for path in out_dir.rglob("*") if path.is_file()]
    if not files:
        return 0
    client = BlobServiceClient.from_connection_string(connection)
    workers = min(12, len(files))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_upload_one, client, container, prefix, path, out_dir)
            for path in files
        ]
        for future in as_completed(futures):
            future.result()
    return len(files)


def _both_renditions(src: Path, out_dir: Path, with_audio: bool) -> None:
    """Decode the source once and write 360p and 720p HLS together."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-filter_complex",
        "[0:v]split=2[v360][v720];[v360]scale=-2:360[o360];[v720]scale=-2:720[o720]",
        "-map", "[o360]",
    ]
    if with_audio:
        cmd += ["-map", "0:a:0"]
    cmd += ["-map", "[o720]"]
    if with_audio:
        cmd += ["-map", "0:a:0"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
        "-b:v:0", "500k", "-maxrate:v:0", "500k", "-bufsize:v:0", "500k",
        "-b:v:1", "1400k", "-maxrate:v:1", "1400k", "-bufsize:v:1", "1400k",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
    ]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    stream_map = "v:0,a:0,name:360 v:1,a:1,name:720" if with_audio else "v:0,name:360 v:1,name:720"
    # Flat names: ffmpeg on Windows cannot create the %v subfolders itself.
    cmd += [
        "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", stream_map,
        "-hls_segment_filename", str(out_dir / "seg_%v_%03d.ts"),
        str(out_dir / "index_%v.m3u8"),
    ]
    _run(cmd)
    _arrange_variants(out_dir)


def _arrange_variants(out_dir: Path) -> None:
    """Move flat ffmpeg output into 360/ and 720/ folders the player expects."""
    master_path = out_dir / "master.m3u8"
    master = master_path.read_text(encoding="utf-8")
    for name in ("360", "720"):
        playlist = out_dir / f"index_{name}.m3u8"
        if not playlist.is_file():
            continue
        dest = out_dir / name
        dest.mkdir(parents=True, exist_ok=True)
        lines = []
        prefix = f"seg_{name}_"
        for line in playlist.read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                renamed = "seg_" + line[len(prefix):]
                source = out_dir / line
                if source.is_file():
                    source.replace(dest / renamed)
                lines.append(renamed)
            else:
                lines.append(line)
        (dest / "index.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")
        playlist.unlink()
        master = master.replace(f"index_{name}.m3u8", f"{name}/index.m3u8")
    master_path.write_text(master, encoding="utf-8")


def _encode_renditions(src: Path, out_dir: Path, with_audio: bool) -> None:
    try:
        _both_renditions(src, out_dir, with_audio)
        if not (out_dir / "master.m3u8").is_file():
            raise RuntimeError("HLS master playlist was not written")
    except Exception:
        logger.exception("Single-pass HLS failed; encoding each quality separately")
        shutil.rmtree(out_dir, ignore_errors=True)
        _rendition(src, 360, "500k", out_dir / "360", with_audio)
        _rendition(src, 720, "1400k", out_dir / "720", with_audio)
        _master(out_dir)


def _public_file_url(container: str, blob_name: str) -> str | None:
    try:
        from app.services.cloudinary_service import _public_url
        return _public_url(container, blob_name)
    except Exception:
        logger.exception("Could not build a public URL for CDN warm-up")
        return None


def _warm_cdn(container: str, prefix: str) -> int:
    """Request the files a phone plays first, so the CDN stores them before the first viewer."""
    master = _public_file_url(container, f"{prefix}/master.m3u8")
    if not master:
        return 0
    root = master.rsplit("/master.m3u8", 1)[0]
    urls = [
        master,
        f"{root}/poster.jpg",
        f"{root}/360/index.m3u8",
        f"{root}/360/seg_000.ts",
        f"{root}/360/seg_001.ts",
        f"{root}/360/seg_002.ts",
    ]
    warmed = 0

    def _get(url: str) -> bool:
        request = urllib.request.Request(url, headers={"User-Agent": "mindsurve-encode-warm"})
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read(64)
            return 200 <= getattr(response, "status", 200) < 400

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_get, url) for url in urls]
        for future in as_completed(futures):
            try:
                if future.result():
                    warmed += 1
            except Exception:
                logger.info("CDN warm-up request failed", exc_info=True)
    return warmed


def encode_hls(source_url: str, output_prefix: str, connection: str, container: str) -> None:
    work = Path(tempfile.mkdtemp(prefix="mindsurve-encode-"))
    started = time.perf_counter()
    try:
        src = work / "source.mp4"
        out = work / "out"
        download_started = time.perf_counter()
        _download(source_url, src, container, connection)
        download_s = time.perf_counter() - download_started
        with_audio = _has_audio(src)
        encode_started = time.perf_counter()
        _encode_renditions(src, out, with_audio)
        _poster(src, out / "poster.jpg")
        encode_s = time.perf_counter() - encode_started
        prefix = output_prefix.strip("/")
        upload_started = time.perf_counter()
        uploaded = _upload_tree(out, container, prefix, connection)
        upload_s = time.perf_counter() - upload_started
        warm_started = time.perf_counter()
        warmed = _warm_cdn(container, prefix)
        warm_s = time.perf_counter() - warm_started
        logger.info(
            "Encoded %s in %.1fs (download %.1fs, ffmpeg %.1fs, %d files uploaded in %.1fs, %d warmed in %.1fs)",
            prefix,
            time.perf_counter() - started,
            download_s,
            encode_s,
            uploaded,
            upload_s,
            warmed,
            warm_s,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
