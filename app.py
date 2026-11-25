"""
yt-dlp Microservice for NovaTube
Extracts YouTube stream info (HLS manifests, progressive formats, audio,
subtitles) via yt-dlp without downloading media.

Env vars
  API_KEY          required. Shared secret for the /hls endpoint (x-api-key header)
                   and for signing proxy tokens.
  COOKIES_CONTENT  optional. Netscape cookies.txt content for yt-dlp.
  PUBLIC_BASE_URL  optional. e.g. https://ytdlp-micro.onrender.com
                   Overrides the base URL used in proxy links.
  TOKEN_TTL        optional. Proxy token lifetime in seconds (default 21600 = 6h).
  CORS_ORIGINS     optional. Comma-separated allowed origins (default "*").
"""

import base64
import glob
import hashlib
import hmac
import os
import re
import shutil
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

app = FastAPI(title="yt-dlp Microservice", version="1.1.0")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "")
COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
TOKEN_TTL = int(os.getenv("TOKEN_TTL", "21600"))
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
# --- Personal download mode -------------------------------------------------
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/data")
MAX_DURATION = int(os.getenv("MAX_DURATION", "7200"))  # seconds; 2h default
ALLOWED_AUDIO_FORMATS = {"m4a", "mp3", "flac", "wav"}

COOKIES_FILE = "/tmp/cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

ALLOWED_HOST_SUFFIXES = ("googlevideo.com", "youtube.com")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["Range", "Content-Type", "x-api-key"],
)

# Write the cookies file once at startup (not on every request).
if COOKIES_CONTENT:
    with open(COOKIES_FILE, "w", encoding="utf-8") as _f:
        _f.write(COOKIES_CONTENT)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def require_api_key(x_api_key: Optional[str]) -> None:
    if not API_KEY or not x_api_key or not hmac.compare_digest(
        x_api_key.encode(), API_KEY.encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _sig(payload: str, exp: int) -> str:
    return hmac.new(API_KEY.encode(), f"{payload}|{exp}".encode(), hashlib.sha256).hexdigest()


def sign(payload: str) -> str:
    """Short-lived token bound to `payload`. Format: <expiry>.<hmac>"""
    exp = int(time.time()) + TOKEN_TTL
    return f"{exp}.{_sig(payload, exp)}"


def verify(payload: str, token: Optional[str]) -> bool:
    if not API_KEY or not token:
        return False
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _sig(payload, exp))


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def extract_video_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{11})",
        r"(?:youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})",
        r"(?:youtube\.com\/v\/)([a-zA-Z0-9_-]{11})",
        r"(?:youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def is_allowed_host(url: str) -> bool:
    """Strict hostname check (a substring check can be bypassed)."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_HOST_SUFFIXES)


def is_playlist_url(url: str) -> bool:
    """True for HLS playlist URLs (variant/media playlists), False for segments."""
    path = urlparse(url).path.lower()
    return ".m3u8" in path or "/manifest/" in path


def proxy_url(request: Request, target: str) -> str:
    base = (
        f"{PUBLIC_BASE_URL}/proxy-m3u8"
        if PUBLIC_BASE_URL
        else str(request.url_for("proxy_m3u8"))
    )
    enc = base64.urlsafe_b64encode(target.encode()).decode()
    return f"{base}?url={enc}&token={sign(enc)}"


_URI_ATTR = re.compile(r'URI="([^"]+)"')


def rewrite_manifest(text: str, manifest_url: str, request: Request) -> str:
    """
    Rewrite playlist URLs to go through this proxy (so CORS headers are added).
    Segment URLs are made absolute and left pointing at googlevideo.com.
    """

    def rewrite_target(raw: str) -> str:
        absolute = urljoin(manifest_url, raw)
        if is_allowed_host(absolute) and is_playlist_url(absolute):
            return proxy_url(request, absolute)
        return absolute

    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            # Tags like #EXT-X-MEDIA / #EXT-X-KEY / #EXT-X-MAP carry URI="..."
            out.append(_URI_ATTR.sub(lambda m: f'URI="{rewrite_target(m.group(1))}"', s))
        else:
            out.append(rewrite_target(s))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------------
def build_ytdlp_options() -> dict:
    """Build yt-dlp options for extracting stream info without downloading."""
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo+bestaudio/best",
        # Return info even if format selection fails, so we can inspect formats.
        "ignore_no_formats_error": True,
        "extractor_args": {
            "youtube": {"player_client": ["web_safari", "ios", "web"]},
        },
    }
    if COOKIES_CONTENT and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts

def build_download_options(fmt: str) -> dict:
    """yt-dlp options for a single audio file. Requires ffmpeg in the image."""
    postprocessors = [{"key": "FFmpegMetadata"}]

    extract = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
    if fmt == "mp3":
        # Only mp3 takes a quality target; m4a is remuxed, not re-encoded.
        extract["preferredquality"] = "192"
    postprocessors.insert(0, extract)

    # Cover art is meaningless in a .wav container.
    if fmt in {"m4a", "mp3", "flac"}:
        postprocessors.append({"key": "EmbedThumbnail"})

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "prefer_ffmpeg": True,
        "writethumbnail": fmt in {"m4a", "mp3", "flac"},
        "postprocessors": postprocessors,
        # Refuse anything longer than MAX_DURATION instead of burning CPU on it.
        "match_filter": lambda info, *args, **kwargs: (
            None
            if (info.get("duration") or 0) <= MAX_DURATION
            else f"longer than {MAX_DURATION}s"
        ),
    }

    if COOKIES_CONTENT and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    return opts


def _has(codec) -> bool:
    return bool(codec) and codec != "none"


def _pick_track(tracks: list) -> Optional[dict]:
    """Prefer WebVTT; otherwise the first track that has a URL."""
    usable = [t for t in tracks if t.get("url")]
    if not usable:
        return None
    for t in usable:
        if t.get("ext") == "vtt":
            return t
    return usable[0]


def _subtitle_entries(source: dict, suffix: str = "") -> list:
    entries = []
    for lang, tracks in (source or {}).items():
        track = _pick_track(tracks or [])
        if track:
            entries.append(
                {
                    "lang": f"{lang}{suffix}",
                    "label": track.get("name") or lang,
                    "url": track["url"],
                }
            )
    return entries


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"service": "yt-dlp microservice", "status": "ok"}


@app.get("/health")
def health():
    """Health check endpoint for uptime monitoring."""
    return {"status": "ok", "ffmpeg": shutil.which("ffmpeg") is not None}

# Plain `def` (not async): yt-dlp is blocking, so FastAPI runs this in a
# threadpool instead of freezing the event loop.
@app.get("/hls")
def get_streams(request: Request, url: str, x_api_key: Optional[str] = Header(None)):
    """
    Get stream information for a YouTube video.
    Requires x-api-key header.
    """
    require_api_key(x_api_key)

    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")

    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    try:
        with yt_dlp.YoutubeDL(build_ytdlp_options()) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=502, detail=f"yt-dlp error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

    if not info:
        raise HTTPException(status_code=404, detail="Video not found or unavailable")

    result = {
        "id": info.get("id", video_id),
        "title": info.get("title", ""),
        "channel": info.get("channel", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration") or 0,
        "is_live": bool(info.get("is_live")),
        "hls": None,
        "hlsProxy": None,
        "progressive": [],
        "audio": [],
        "hlsSubtitles": [],
    }

    # ---- Sort formats into buckets ----
    hls_formats, muxed, video_only, audio_only = [], [], [], []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        has_v, has_a = _has(f.get("vcodec")), _has(f.get("acodec"))
        if (f.get("protocol") or "").startswith("m3u8"):
            hls_formats.append(f)
        elif has_v and has_a:
            muxed.append(f)
        elif has_v:
            video_only.append(f)
        elif has_a:
            audio_only.append(f)

    by_height = lambda f: f.get("height") or 0  # noqa: E731
    by_abr = lambda f: f.get("abr") or 0  # noqa: E731
    muxed.sort(key=by_height, reverse=True)
    video_only.sort(key=by_height, reverse=True)
    audio_only.sort(key=by_abr, reverse=True)

    result["progressive"] = [
        {
            "url": f["url"],
            "height": f.get("height") or 0,
            "width": f.get("width") or 0,
            "ext": f.get("ext", "mp4"),
            "vcodec": f.get("vcodec", ""),
            "acodec": f.get("acodec", ""),
            "fps": f.get("fps") or 0,
            "filesize": f.get("filesize") or f.get("filesize_approx") or 0,
        }
        for f in muxed
    ]
    result["audio"] = [
        {
            "url": f["url"],
            "ext": f.get("ext", "m4a"),
            "acodec": f.get("acodec", ""),
            "abr": f.get("abr") or 0,
        }
        for f in audio_only
    ]

    # ---- Backwards-compatible "best" fields ----
    best_video = (muxed or video_only or [None])[0]
    if best_video:
        ext = best_video.get("ext", "mp4")
        result.update(
            {
                "url": best_video["url"],
                "height": best_video.get("height") or 0,
                "width": best_video.get("width") or 0,
                "ext": ext,
                "mime_type": f"video/{ext}",
                "vcodec": best_video.get("vcodec", ""),
                "fps": best_video.get("fps") or 0,
                "filesize": best_video.get("filesize") or best_video.get("filesize_approx") or 0,
            }
        )
    if audio_only:
        a = audio_only[0]
        ext = a.get("ext", "m4a")
        result.update(
            {
                "audio_url": a["url"],
                "audio_ext": ext,
                "audio_mime": f"audio/{ext}",
                "audio_bitrate": a.get("abr") or 0,
                "audio_codec": a.get("acodec", ""),
            }
        )

    # ---- HLS: use the real manifest yt-dlp found, never a synthesized URL ----
    master = info.get("manifest_url") or next(
        (f["manifest_url"] for f in hls_formats if f.get("manifest_url")), None
    )
    if not master and hls_formats:
        # Fall back to the highest-quality single variant playlist.
        master = max(hls_formats, key=by_height)["url"]

    if master:
        result["hls"] = master
        if is_allowed_host(master):
            result["hlsProxy"] = proxy_url(request, master)

    # ---- Subtitles ----
    result["hlsSubtitles"] = _subtitle_entries(info.get("subtitles")) + _subtitle_entries(
        info.get("automatic_captions"), suffix=" (auto)"
    )

    return result

@app.get("/download")
def download_audio(
    url: str,
    fmt: str = "m4a",
    x_api_key: Optional[str] = Header(None),
):
    """
    Download a single audio track. Requires x-api-key.

    fmt: m4a (native, no re-encode) | mp3 | flac | wav
    The file is written to DOWNLOAD_DIR and served with Range support.
    """
    require_api_key(x_api_key)

    fmt = (fmt or "").lower()
    if fmt not in ALLOWED_AUDIO_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format. Use one of: {', '.join(sorted(ALLOWED_AUDIO_FORMATS))}",
        )

    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=501, detail="ffmpeg is not installed in this image")

    try:
        with yt_dlp.YoutubeDL(build_download_options(fmt)) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=True
            )
            final = info.get("filepath") or ydl.prepare_filename(info)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=502, detail=f"yt-dlp error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

    # prepare_filename() reports the pre-postprocessor extension.
    if not os.path.isfile(final):
        base, _ = os.path.splitext(final)
        if os.path.isfile(f"{base}.{fmt}"):
            final = f"{base}.{fmt}"

    if not os.path.isfile(final):
        # Last resort: find whatever yt-dlp actually wrote for this video id.
        found = [
            p
            for p in glob.glob(os.path.join(DOWNLOAD_DIR, f"*[{video_id}].*"))
            if not p.endswith((".webp", ".jpg", ".jpeg", ".png", ".part"))
        ]
        if not found:
            raise HTTPException(status_code=500, detail="Downloaded file not found")
        final = max(found, key=os.path.getmtime)

    media_types = {
        "m4a": "audio/mp4",
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
    }

    return FileResponse(
        final,
        media_type=media_types[fmt],
        filename=os.path.basename(final),
    )

@app.get("/proxy-m3u8")
async def proxy_m3u8(request: Request, url: str, token: Optional[str] = None):
    """
    Proxy an HLS playlist so browsers can load it (adds CORS headers).
    Requires a short-lived signed token (issued by /hls), not the API key.
    Playlist URLs are rewritten to this proxy; segments stay absolute.
    """
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")

    if not verify(url, token):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    try:
        decoded_url = base64.urlsafe_b64decode(url).decode("utf-8")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL encoding")

    if not is_allowed_host(decoded_url):
        raise HTTPException(status_code=400, detail="Only YouTube/googlevideo HTTPS URLs are allowed")

    async with httpx.AsyncClient(follow_redirects=False) as client:
        try:
            resp = await client.get(decoded_url, timeout=30.0)
            resp.raise_for_status()
            text = resp.text
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch manifest: {e}")

    return PlainTextResponse(
        content=rewrite_manifest(text, decoded_url, request),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )
