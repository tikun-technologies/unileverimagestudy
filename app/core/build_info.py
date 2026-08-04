"""Build/deploy identity, read once at process startup.

Lets us tell from a live response (header or /health) exactly which image is
serving traffic, instead of guessing whether a deploy actually landed.

APP_VERSION: optional, set via `docker build --build-arg APP_VERSION=v72 ...`.
    Defaults to "unknown" if the build didn't pass it.
BUILD_TIME: written to /app/BUILD_TIME by a RUN step placed after `COPY . .`
    in Dockerfile.api. Docker only re-executes that RUN when the COPY layer
    before it changed, i.e. whenever the source actually differs — so this
    timestamp updates automatically on every real code change, with no extra
    flags required on `docker build`.
"""

import os

APP_VERSION: str = os.getenv("APP_VERSION", "unknown")

_BUILD_TIME_FILE = "/app/BUILD_TIME"
try:
    with open(_BUILD_TIME_FILE, "r", encoding="utf-8") as _f:
        BUILD_TIME: str = _f.read().strip() or "unknown"
except OSError:
    BUILD_TIME = "unknown"
