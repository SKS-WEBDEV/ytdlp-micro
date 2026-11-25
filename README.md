# ytdlp-micro

A small FastAPI service that turns a YouTube URL into playable stream
information — HLS manifests, progressive MP4s, audio-only tracks and
subtitles — using [yt-dlp](https://github.com/yt-dlp/yt-dlp). It extracts
metadata only; nothing is downloaded or stored.

It also proxies HLS **playlists** (not media bytes) so a browser can load
them with the CORS headers `googlevideo.com` does not send.

Runs as a single Docker container. Deployed on [Render](https://render.com).

---

## What it does

| | |
|---|---|
| Input | A YouTube watch / youtu.be / embed / shorts URL |
| Output | JSON with metadata, a signed HLS proxy URL, format lists, subtitles |
| Storage | None — every request is stateless |
| Auth | `x-api-key` header on `/hls`; short-lived HMAC tokens on `/proxy-m3u8` |
| Media proxying | Playlists only. Segments are fetched by the client directly |

---

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | none | Service banner |
| `GET` | `/health` | none | Health probe for Render |
| `GET` | `/hls` | `x-api-key` | Resolve a video to streams |
| `GET` | `/proxy-m3u8` | signed token | Serve a rewritten HLS playlist |

### `GET /hls`

```
GET /hls?url=https://www.youtube.com/watch?v=VIDEO_ID
x-api-key: <API_KEY>
```

The URL may be any of these forms; the 11-character video ID is extracted
with a regex:

```
https://www.youtube.com/watch?v=VIDEO_ID
https://youtu.be/VIDEO_ID
https://www.youtube.com/embed/VIDEO_ID
https://www.youtube.com/v/VIDEO_ID
https://www.youtube.com/shorts/VIDEO_ID
```

Response (abridged):

```json
{
  "id": "dQw4w9WgXcQ",
  "title": "Example",
  "channel": "Example Channel",
  "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
  "duration": 213,
  "is_live": false,
  "hls": "https://manifest.googlevideo.com/api/manifest/hls_variant/...",
  "hlsProxy": "https://your-service.onrender.com/proxy-m3u8?url=...&token=...",
  "progressive": [
    {
      "url": "https://rr1---sn-....googlevideo.com/videoplayback?...",
      "height": 720,
      "width": 1280,
      "ext": "mp4",
      "vcodec": "avc1.64001F",
      "acodec": "mp4a.40.2",
      "fps": 30,
      "filesize": 18432000
    }
  ],
  "audio": [
    { "url": "https://...", "ext": "m4a", "acodec": "mp4a.40.2", "abr": 128 }
  ],
  "hlsSubtitles": [
    { "lang": "en", "label": "English", "url": "https://..." }
  ]
}
```

For backwards compatibility the response also carries the best format
flattened onto the top level: `url`, `height`, `width`, `ext`, `mime_type`,
`vcodec`, `fps`, `filesize`, `audio_url`, `audio_ext`, `audio_mime`,
`audio_bitrate`, `audio_codec`.

**Use `hlsProxy`, never `hls`.** The raw manifest URL is bound to this
service's egress IP; the proxy URL is signed and carries the CORS headers a
browser needs.

| Status | Meaning |
|---|---|
| `400` | Missing `url`, or not a recognisable YouTube URL |
| `401` | Missing or wrong `x-api-key` |
| `404` | Video unavailable or removed |
| `502` | yt-dlp could not extract the video (bot check, geo-block, age gate) |
| `500` | Unexpected error |

### `GET /proxy-m3u8`

```
GET /proxy-m3u8?url=<base64url target>&token=<expiry>.<hmac>
```

`/hls` issues this URL for you; you normally do not build it by hand.

* `url` — URL-safe base64 of the playlist address.
* `token` — `<unix expiry>.<hex hmac-sha256>` computed over
  `<base64url>|<expiry>` using `API_KEY`, valid for `TOKEN_TTL` seconds.
* Only `https` URLs on `googlevideo.com` or `youtube.com` are accepted, so a
  forged token cannot turn the service into an open proxy.

The playlist is fetched and rewritten so that **variant playlists, keys and
init segments** point back at this proxy, while **media segments** stay
absolute and are fetched directly by the player. Response headers:

```
Content-Type: application/vnd.apple.mpegurl
Cache-Control: no-store
```

---

## Quick start (local)

```bash
git clone https://github.com/SKS-WEBDEV/ytdlp-micro.git
cd ytdlp-micro

docker build -t ytdlp-micro .
docker run --rm -p 8000:8000 \
  -e API_KEY=dev-secret \
  -e PUBLIC_BASE_URL=http://localhost:8000 \
  ytdlp-micro
```

Then:

```bash
curl -s -H "x-api-key: dev-secret" \
  "http://localhost:8000/hls?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ" | jq
```

Without Docker:

```bash
pip install -r requirements.txt
API_KEY=dev-secret PUBLIC_BASE_URL=http://localhost:8000 sh start.sh
```

---

## Deploy to Render

1. **New → Web Service**, connect this repository.
2. Runtime: **Docker**. No build or start command is needed — the
   `Dockerfile` installs dependencies and `start.sh` launches uvicorn.
3. **Health check path:** `/health`.
4. Add the environment variables below, then deploy.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `API_KEY` | **yes** | — | Shared secret for `/hls`. Also signs proxy tokens. Use 32+ random bytes. |
| `PUBLIC_BASE_URL` | strongly recommended | — | Public origin, e.g. `https://ytdlp-micro.onrender.com`. Without it proxy links are built from the request URL and can come out as `http://`. |
| `COOKIES_CONTENT` | optional | — | Netscape-format `cookies.txt` contents, written to `/tmp/cookies.txt` at boot. Needed when YouTube answers with a bot check. |
| `TOKEN_TTL` | optional | `21600` | Proxy token lifetime in seconds (6 hours). |
| `CORS_ORIGINS` | optional | `*` | Comma-separated allowed origins. Irrelevant if only your backend calls `/hls`. |

`PORT` is supplied by Render and honoured by `start.sh`.

Generate a key:

```bash
openssl rand -hex 32
```

### Cookies

When yt-dlp reports *"Sign in to confirm you're not a bot"*, export cookies
from a **dedicated throwaway Google account** using a `cookies.txt`
extension, and paste the file contents into `COOKIES_CONTENT`.

Treat that value as a credential: anyone with dashboard access can read it.

---

## How playback works

```
Browser            Your backend            ytdlp-micro           googlevideo.com
   |                    |                       |                      |
   |-- want video ----->|                       |                      |
   |                    |-- /hls + api key ---->|                      |
   |                    |                       |-- yt-dlp extract --->|
   |                    |<-- json + hlsProxy ---|                      |
   |<-- json -----------|                       |                      |
   |-- hlsProxy ------------------------------>|                      |
   |<-- rewritten playlist (CORS) -------------|                      |
   |-- segment URLs -------------------------------------------------->|
   |<-- media bytes ---------------------------------------------------|
```

Only the playlist passes through this service. Media bytes go straight from
`googlevideo.com` to the viewer, so Render bandwidth stays near zero.

**Never call `/hls` from the browser** — the API key would be exposed.
Call it from your server, cache the result, and hand only `hlsProxy` to the
client.

---

## Security model

* `/hls` requires a constant-time comparison against `API_KEY`.
* Proxy tokens are HMAC-signed and expire; they are not reusable forever.
* `is_allowed_host()` parses the hostname and requires an exact or
  subdomain match on `googlevideo.com` / `youtube.com`, with `https` only.
  A substring check would be bypassable; this is not one.
* Base64 payloads are decoded after signature verification.
* No filesystem writes except `/tmp/cookies.txt` at startup.

Not included: per-client rate limiting, request logging, metrics. Put this
behind a reverse proxy or a private network if it will face the public
internet.

---

## Operational notes

* **Cold starts.** The Render free tier sleeps after 15 minutes idle; the
  first request then takes 30–90 seconds. Use a paid instance, or ping
  `/health` every 10 minutes.
* **Memory.** 512 MB fits one extraction comfortably; concurrent blocking
  extractions in FastAPI's threadpool can exhaust it. Cap concurrency if you
  expect parallel traffic.
* **Segment fetching.** Media segments are left absolute by design. If
  `googlevideo.com` binds them to this service's IP, the browser receives
  `403`. Test from a different network; if it fails, proxy segments too (and
  budget for bandwidth) or fall back to the official embed.
* **Manifest redirects.** `/proxy-m3u8` does not follow redirects. If
  YouTube answers a playlist with a `3xx`, the rewritten body is empty and
  the player fails with a parse error.
* **Dependency drift.** `start.sh` upgrades yt-dlp on every boot, which keeps
  up with YouTube changes but makes deploys non-reproducible. Pin a version
  in `requirements.txt` if you need deterministic builds.
* **Availability.** yt-dlp extraction depends on YouTube's private responses
  and breaks periodically. Watch `/health` and the `502` rate.

---

## Disclaimer

This project is for personal and educational use. Extracting streams with
yt-dlp and re-serving YouTube content is outside YouTube's Terms of Service
and may infringe copyright. You are responsible for how you deploy it. If
you need a compliant setup, use the official IFrame Player or the YouTube
Data API.

---

## License

No license file is present in this repository. Add one before distributing
or accepting contributions.
