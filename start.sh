#!/bin/bash
# Keep yt-dlp current: YouTube changes often and old versions break.
pip install -q --no-cache-dir --upgrade yt-dlp

# --proxy-headers lets Render's TLS proxy tell uvicorn the real scheme (https),
# so generated proxy links use https instead of http.
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips='*'
