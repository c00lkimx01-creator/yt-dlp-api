import os
import tempfile
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import httpx

app = FastAPI(title="yt-dlp API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Anti bot-detection 設定
# ---------------------------------------------------------------------------
# YouTube は datacenter IP / Render などからのアクセスに対して
# "Sign in to confirm you're not a bot" を返してくる。
# Cookie 無しで通すには以下が有効:
#   - player_client を web 以外 (tv / ios / mweb / android_vr) に切り替える
#   - po_token を要求しないクライアントを優先
#   - 適切な User-Agent をセット
#   - リトライ
# それでも失敗する場合のために、環境変数 YT_COOKIES (Netscape形式の中身)
# を渡せばそれを使う。
# ---------------------------------------------------------------------------

_COOKIE_FILE: str | None = None


def _prepare_cookie_file() -> str | None:
    global _COOKIE_FILE
    if _COOKIE_FILE:
        return _COOKIE_FILE
    raw = os.environ.get("YT_COOKIES")
    if not raw:
        return None
    fd, path = tempfile.mkstemp(prefix="ytcookies_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if not raw.lstrip().startswith("# Netscape"):
            f.write("# Netscape HTTP Cookie File\n")
        f.write(raw)
    _COOKIE_FILE = path
    return path


# Cookie 無しで bot 判定を回避しやすいクライアント順。
# tv_embedded / ios / mweb は po_token なしでも動きやすい。
PLAYER_CLIENT_FALLBACKS = [
    ["tv", "ios", "mweb"],
    ["ios", "mweb"],
    ["android_vr"],
    ["web_safari"],
    ["web"],
]

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


def _base_opts(player_clients: list[str]) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 20,
        "http_headers": {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {
                "player_client": player_clients,
                # skip 不要な hls/dash 取得を抑制しすぎないように何も skip しない
            }
        },
    }
    cookies = _prepare_cookie_file()
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def extract_info(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    last_err: Exception | None = None
    for clients in PLAYER_CLIENT_FALLBACKS:
        opts = _base_opts(clients)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            last_err = e
            # bot 判定 / sign in 系なら次のクライアントを試す
            if (
                "Sign in to confirm" in msg
                or "confirm you" in msg
                or "bot" in msg.lower()
                or "Requested format" in msg
                or "Failed to extract" in msg
            ):
                continue
            raise HTTPException(status_code=404, detail=msg)
        except Exception as e:
            last_err = e
            continue
    detail = f"All player clients failed: {last_err}"
    raise HTTPException(status_code=429, detail=detail)


@app.get("/")
def root():
    return {
        "name": "yt-dlp API",
        "endpoints": [
            "/api/streams/{video_id}",
            "/api/streams/{video_id}/m3u8",
            "/api/streams/{video_id}/m3u8/raw",
        ],
        "cookies_loaded": bool(os.environ.get("YT_COOKIES")),
    }


@app.get("/api/streams/{video_id}")
def get_streams(video_id: str):
    info = extract_info(video_id)
    formats = []
    for f in info.get("formats", []):
        if not f.get("url"):
            continue
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "protocol": f.get("protocol"),
            "resolution": f.get("resolution"),
            "width": f.get("width"),
            "height": f.get("height"),
            "fps": f.get("fps"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "tbr": f.get("tbr"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "url": f.get("url"),
        })
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "is_live": info.get("is_live", False),
        "formats": formats,
    }


@app.get("/api/streams/{video_id}/m3u8")
def get_m3u8(video_id: str):
    info = extract_info(video_id)
    hls = [
        {
            "format_id": f.get("format_id"),
            "resolution": f.get("resolution"),
            "height": f.get("height"),
            "fps": f.get("fps"),
            "tbr": f.get("tbr"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "url": f.get("url"),
        }
        for f in info.get("formats", [])
        if (f.get("protocol") or "").startswith("m3u8")
        or (f.get("url") or "").endswith(".m3u8")
        or "m3u8" in (f.get("url") or "")
    ]
    if not hls:
        raise HTTPException(status_code=404, detail="No m3u8 streams available")
    hls.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0), reverse=True)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "is_live": info.get("is_live", False),
        "best": hls[0],
        "streams": hls,
    }


@app.get("/api/streams/{video_id}/m3u8/raw")
async def get_m3u8_raw(video_id: str):
    info = extract_info(video_id)
    hls_urls = [
        f.get("url")
        for f in info.get("formats", [])
        if (f.get("protocol") or "").startswith("m3u8") and f.get("url")
    ]
    if not hls_urls:
        raise HTTPException(status_code=404, detail="No m3u8 streams available")
    url = hls_urls[-1]
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": DEFAULT_UA})
    return Response(content=r.content, media_type="application/vnd.apple.mpegurl")
