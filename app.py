"""
yt-dlp Microservice for NovaTube
Extracts YouTube HLS manifests via yt-dlp without downloading media.
"""

import os
import base64
import re
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx
import yt_dlp

app = FastAPI(title="yt-dlp Microservice", version="1.0.0")

# Configuration
API_KEY = os.getenv("API_KEY", "")
COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")

# Temporary cookies file path
COOKIES_FILE = "/tmp/cookies.txt"


def write_cookies_file():
    """Write cookies from env var to temp file for yt-dlp."""
    if COOKIES_CONTENT:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(COOKIES_CONTENT)


def extract_video_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com\/v\/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def build_ytdlp_options() -> dict:
    """Build yt-dlp options for extracting stream info without downloading."""
    opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'format': 'bestvideo+bestaudio/best',
    }

    # Add cookies file if available
    if COOKIES_CONTENT and os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE

    # Add client options to bypass bot detection
    opts['extractor_args'] = {
        'youtube': {
            'player_client': ['web_safari', 'ios', 'web'],
        }
    }

    return opts


@app.get("/health")
async def health():
    """Health check endpoint for uptime monitoring."""
    return {"status": "ok"}


@app.get("/hls")
async def get_streams(url: str, x_api_key: str = Header(None)):
    """
    Get stream information for a YouTube video.
    Requires x-api-key header.
    """
    # Validate API key
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")

    # Extract video ID
    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    # Write cookies file if available
    write_cookies_file()

    # Build yt-dlp options
    ydl_opts = build_ytdlp_options()

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)

            if not info:
                raise HTTPException(status_code=404, detail="Video not found or unavailable")

            # Build stream info result
            result = {
                "id": info.get("id", video_id),
                "title": info.get("title", ""),
                "channel": info.get("channel", ""),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration", 0),
                "is_live": info.get("is_live", False),
                "hls": None,
                "hlsProxy": None,
                "progressive": [],
                "audio": [],
                "hlsSubtitles": [],
            }

            # Extract formats
            formats = info.get('formats', [])
            best_video = None
            best_audio = None

            for f in formats:
                if not f.get('url'):
                    continue

                has_video = f.get('vcodec') and f['vcodec'] != 'none'
                has_audio = f.get('acodec') and f['acodec'] != 'none'

                if not has_video and not has_audio:
                    continue

                # Video + Audio (muxed)
                if has_video and has_audio:
                    if not best_video:
                        best_video = f
                    elif f.get('height', 0) > best_video.get('height', 0):
                        best_video = f

                # Video only
                elif has_video and not has_audio:
                    if not best_video or (f.get('height', 0) > best_video.get('height', 0)):
                        best_video = f

                # Audio only
                elif has_audio and not has_video:
                    if not best_audio:
                        best_audio = f

            # Build stream info from best video
            if best_video:
                stream = {
                    'url': best_video.get('url', ''),
                    'height': best_video.get('height', 0),
                    'width': best_video.get('width', 0),
                    'ext': best_video.get('ext', 'mp4'),
                    'mime_type': best_video.get('mime_type', 'video/mp4'),
                    'vcodec': best_video.get('vcodec', ''),
                    'fps': best_video.get('fps', 0),
                    'filesize': best_video.get('filesize', 0),
                }
                result.update(stream)

            # Add audio info
            if best_audio:
                result['audio_url'] = best_audio.get('url', '')
                result['audio_ext'] = best_audio.get('ext', 'm4a')
                result['audio_mime'] = best_audio.get('mime_type', 'audio/mp4')
                result['audio_bitrate'] = best_audio.get('abr', 0)
                result['audio_codec'] = best_audio.get('acodec', '')

            # Build HLS master URL (synthesized from YouTube manifest)
            master_url = f"https://manifest.googlevideo.com/api/manifest/hls_variant/expire/0/ei/0/id/{video_id}"
            result['hls'] = master_url

            # Build proxy URL for the master
            master_encoded = base64.urlsafe_b64encode(master_url.encode()).decode()
            result['hlsProxy'] = f"{request.url_for('proxy_m3u8')}?url={master_encoded}&key={API_KEY}"

            # Build subtitle tracks
            subtitles = info.get('subtitles', {})
            for lang, tracks in subtitles.items():
                for track in tracks:
                    if track.get('url'):
                        result['hlsSubtitles'].append({
                            'lang': lang,
                            'label': track.get('name', lang),
                            'url': track['url'],
                        })

            # Automatic captions
            auto_captions = info.get('automatic_captions', {})
            for lang, tracks in auto_captions.items():
                if any(t.get('url') for t in tracks):
                    lang_key = f"{lang} (auto)"
                    if lang_key not in [s['lang'] for s in result['hlsSubtitles']]:
                        result['hlsSubtitles'].append({
                            'lang': lang_key,
                            'label': track.get('name', lang),
                            'url': tracks[0]['url'],
                        })

            return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {str(e)}")


@app.get("/proxy-m3u8")
async def proxy_m3u8(url: str, key: str = None, request: Request = None):
    """
    Proxy HLS manifest to bypass CORS restrictions.
    Rewrites manifest URLs to go through this proxy.
    Segments stay absolute (direct browser to googlevideo.com).
    """
    # Validate API key
    if not API_KEY or key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")

    # Decode base64 URL
    try:
        decoded_url = base64.urlsafe_b64decode(url).decode('utf-8')
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL encoding")

    # Validate it's a YouTube/Google URL
    if 'googlevideo.com' not in decoded_url and 'youtube.com' not in decoded_url:
        raise HTTPException(status_code=400, detail="Only YouTube URLs are allowed")

    # Fetch manifest
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(decoded_url, timeout=30.0)
            resp.raise_for_status()
            text = resp.text
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch manifest: {str(e)}")

    # Rewrite manifest URLs to go through this proxy
    lines = text.split('\n')
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and 'googlevideo.com' in stripped:
            # Rewrite manifest URL to go through proxy
            proxy_url = request.url_for("proxy_m3u8")
            encoded_url = base64.urlsafe_b64encode(stripped.encode()).decode()
            
            new_lines.append(f"{proxy_url}?url={encoded_url}&key={API_KEY}")
        else:
            new_lines.append(line)

    text = '\n'.join(new_lines)

    return PlainTextResponse(
        content=text,
        media_type='application/vnd.apple.mpegurl',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Range, Content-Type',
            'Cache-Control': 'no-store',
        }
    )
