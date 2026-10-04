# MindSurve video encode worker

This Container App is a Celery worker. It is not a Container Apps Job and
not a bare ffmpeg image.

```text
localhost or API  →  RabbitMQ queue `video`  →  this worker
                                              →  ffmpeg HLS
                                              →  Blob + PostgreSQL
```

The existing Celery VM keeps doing task generation. It must not consume `video`.

## Blob CORS for browser playback

Chrome plays HLS through `hls.js`, which fetches playlists and segments from
Azure Blob Storage. FastAPI's CORS setting does not apply to those Blob
requests. Add/merge a Blob-service CORS rule for the frontend origins with
`GET`, `HEAD`, and `OPTIONS`, allowed request headers `*`, and exposed headers
`Content-Length`, `Content-Range`, `Accept-Ranges`, and `ETag`. Do not clear
existing rules. Check the current rules first; Azure allows up to five rules.

With Azure CLI installed and the storage connection string set in the current
PowerShell session, the rule can be added with:

```powershell
az storage cors add `
  --services b `
  --methods GET HEAD OPTIONS `
  --origins https://mindsurve.com https://www.mindsurve.com http://localhost:3000 `
  --allowed-headers "*" `
  --exposed-headers Content-Length Content-Range Accept-Ranges ETag `
  --max-age 3600 `
  --connection-string $env:AZURE_STORAGE_CONNECTION_STRING
```

Use only the origins where the frontend is actually hosted. Then verify with
`az storage cors list --services b --connection-string $env:AZURE_STORAGE_CONNECTION_STRING`.

## Build (from the backend repo root)

```powershell
cd D:\TikunTech\uniliverimagestudy
docker build -f deploy/video-encode/Dockerfile -t tusharpareenja/mindsurve-video-encode:1 .
docker push tusharpareenja/mindsurve-video-encode:1
```

## Container App

- Image: `tusharpareenja/mindsurve-video-encode:1`
- Command: already in the image (`celery -A app.celery_app worker -Q video --concurrency=1`)
- CPU / memory: 2 vCPU, 4 GB
- Scale: 1 replica to start. Later, 0 → N on RabbitMQ queue `video`.

## Env on the Container App

Same values as the API. Settings requires a few unused fields on import.

```env
DATABASE_URL=
SECRET_KEY=
JWT_SECRET_KEY=
FROM_EMAIL=unused@localhost
CELERY_BROKER_URL=
AZURE_STORAGE_CONNECTION_STRING=
AZURE_STORAGE_CONTAINER_NAME=mindsurvey
VIDEO_ENCODE_ENABLED=true
```

Do not add callback URLs, tenant IDs, or job names.

## API `.env`

```env
VIDEO_ENCODE_ENABLED=true
```

Storage and `CELERY_BROKER_URL` are already there. Restart the API after you set the flag.

Until the worker is running, leave `VIDEO_ENCODE_ENABLED=false`. Uploads stay MP4.
