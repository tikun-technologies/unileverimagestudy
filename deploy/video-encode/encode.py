"""Optional local CLI. Production uses the Celery worker on queue `video`."""
from __future__ import annotations

import os
import sys

from app.services.video_transcode import encode_hls


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def main() -> int:
    try:
        encode_hls(
            source_url=_env("SOURCE_URL"),
            output_prefix=_env("OUTPUT_PREFIX"),
            connection=_env("AZURE_STORAGE_CONNECTION_STRING"),
            container=_env("AZURE_STORAGE_CONTAINER"),
        )
        return 0
    except Exception as exc:
        print(f"ENCODE FAILED: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
