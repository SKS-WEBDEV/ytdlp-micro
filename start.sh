#!/bin/bash
pip install --upgrade yt-dlp
exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}