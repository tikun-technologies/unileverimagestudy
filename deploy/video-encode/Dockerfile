# MindSurve video Celery worker. ffmpeg is the encoder; this image
# consumes the `video` RabbitMQ queue. Do not deploy a bare ffmpeg image.
# Build from the backend repo root:
#   docker build -f deploy/video-encode/Dockerfile -t tusharpareenja/mindsurve-video-encode:1 .
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# psycopg2 has no wheel here and the slim image cannot compile it.
RUN sed 's/^psycopg2==/psycopg2-binary==/' requirements.txt > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
CMD ["celery", "-A", "app.celery_app", "worker", "-Q", "video", "--concurrency=1", "--loglevel=info"]
