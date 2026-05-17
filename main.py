import base64
import os
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx
import yt_dlp

app = FastAPI(title="yt-dlp API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# YouTube bot判定について
# ---------------------------------------------------------------------------
# Render等のdatacenter IPでは、Cookieなしで完全に回避することはできません。
# このAPIは以下を自動化します:
#   1. po_token不要寄りの player_client フォールバック
#   2. User-Agent / geo_bypass / retries
#   3. YT_COOKIES / YT_COOKIES_B64 / YT_COOKIES_URL / YT_COOKIES_FILE から
#      Cookieを自動ロード・自動更新
#
# 重要: YouTubeログインCookieをサーバーが勝手に取得することはできません。
# 必ず自分のブラウザからエクスポートしたCookie、または自分で管理する
# プライベートCookie URLを環境変数に設定してください。
# ---------------------------------------------------------------------------

COOKIE_REFRESH_SECONDS = int(os.environ.get("YT_COOKIES_REFRESH_SECONDS", "1800"))
_COOKIE_FILE: str | None = None
_COOKIE_SOURCE_SIGNATURE: str | None = None
_COOKIE_LOADED_AT = 0.0

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)

# Cookie無しでbot判定を回避しやすいクライアント順。
PLAYER_CLIENT_FALLBACKS = [
    ["tv", "ios", "mweb"],
    ["ios", "mweb"],
    ["android_vr"],
    ["web_safari"],
    ["web"],
]


def _normalise_cookie_text(raw: str) -> str:
    text = raw.replace("\\r\\n", "\n").replace("\\n", "\n").strip()
    if not text:
        return text
    if not text.lstrip().startswith("# Netscape"):
        text = "# Netscape HTTP Cookie File\n" + text
    return text + "\n"


def _cookie_source_signature() -> str:
    parts = [
        os.environ.get("YT_COOKIES", ""),
        os.environ.get("YT_COOKIES_B64", ""),
        os.environ.get("YT_COOKIES_URL", ""),
        os.environ.get("YT_COOKIES_FILE", ""),
    ]
    return "|".join(parts)


def _write_cookie_file(cookie_text: str) -> str | None:
    global _COOKIE_FILE
    cookie_text = _normalise_cookie_text(cookie_text)
    if not cookie_text.strip():
        return None

    if not _COOKIE_FILE:
        fd, path = tempfile.mkstemp(prefix="ytcookies_", suffix=".txt")
        os.close(fd)
        _COOKIE_FILE = path

    Path(_COOKIE_FILE).write_text(cookie_text, encoding="utf-8")
    return _COOKIE_FILE


def _load_cookie_text_from_env() -> str | None:
    raw = os.environ.get("YT_COOKIES")
    if raw:
        return raw

    raw_b64 = os.environ.get("YT_COOKIES_B64")
    if raw_b64:
        try:
            return base64.b64decode(raw_b64).decode("utf-8")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Invalid YT_COOKIES_B64: {exc}")

    cookie_file = os.environ.get("YT_COOKIES_FILE")
    if cookie_file:
        path = Path(cookie_file)
        if path.exists():
            return path.read_text(encoding="utf-8")
        raise HTTPException(status_code=500, detail=f"YT_COOKIES_FILE not found: {cookie_file}")

    return None


async def _load_cookie_text_from_url() -> str | None:
    url = os.environ.get("YT_COOKIES_URL")
    if not url:
        return None

    headers = {"User-Agent": DEFAULT_UA}
    token = os.environ.get("YT_COOKIES_URL_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch YT_COOKIES_URL: HTTP {response.status_code}",
        )
    return response.text


async def prepare_cookie_file(force_refresh: bool = False) -> str | None:
    """Cookieを環境変数/ファイル/URLから自動ロードしてyt-dlp用ファイルにする。"""
    global _COOKIE_LOADED_AT, _COOKIE_SOURCE_SIGNATURE

    now = time.time()
    signature = _cookie_source_signature()
    should_refresh = (
        force_refresh
        or not _COOKIE_FILE
        or signature != _COOKIE_SOURCE_SIGNATURE
        or (os.environ.get("YT_COOKIES_URL") and now - _COOKIE_LOADED_AT > COOKIE_REFRESH_SECONDS)
    )
    if not should_refresh:
        return _COOKIE_FILE

    cookie_text = _load_cookie_text_from_env()
    if cookie_text is None:
        cookie_text = await _load_cookie_text_from_url()

    _COOKIE_SOURCE_SIGNATURE = signature
    _COOKIE_LOADED_AT = now
    if cookie_text is None:
        return None
    return _write_cookie_file(cookie_text)


def _base_opts(player_clients: list[str], cookie_file: str | None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "retries": 3,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        "http_headers": {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {
                "player_client": player_clients,
            }
        },
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


async def extract_info(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cookie_file = await prepare_cookie_file()
    last_err: Exception | None = None

    for clients in PLAYER_CLIENT_FALLBACKS:
        opts = _base_opts(clients, cookie_file)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc)
            last_err = exc
            if _is_retryable_youtube_error(msg):
                continue
            raise HTTPException(status_code=404, detail=msg)
        except Exception as exc:
            last_err = exc
            continue

    # Cookie URLを使っている場合は、bot判定時に一度だけ強制再取得して再試行。
    if os.environ.get("YT_COOKIES_URL"):
        refreshed_cookie_file = await prepare_cookie_file(force_refresh=True)
        if refreshed_cookie_file and refreshed_cookie_file != cookie_file:
            for clients in PLAYER_CLIENT_FALLBACKS:
                try:
                    with yt_dlp.YoutubeDL(_base_opts(clients, refreshed_cookie_file)) as ydl:
                        return ydl.extract_info(url, download=False)
                except Exception as exc:
                    last_err = exc

    detail = (
        f"All player clients failed: {last_err}. "
        "RenderのIPがYouTubeにbot判定されています。"
        "自分のブラウザでエクスポートしたNetscape形式Cookieを "
        "YT_COOKIES / YT_COOKIES_B64 / YT_COOKIES_URL に設定してください。"
    )
    raise HTTPException(status_code=429, detail=detail)


def _is_retryable_youtube_error(message: str) -> bool:
    lower = message.lower()
    return any(
        word in lower
        for word in [
            "sign in to confirm",
            "confirm you",
            "bot",
            "requested format",
            "failed to extract",
            "precondition check failed",
            "not a valid url",
            "http error 403",
            "http error 429",
        ]
    )


@app.get("/")
async def root():
    cookie_file = await prepare_cookie_file()
    return {
        "name": "yt-dlp API",
        "endpoints": [
            "/api/streams/{video_id}",
            "/api/streams/{video_id}/m3u8",
            "/api/streams/{video_id}/m3u8/raw",
        ],
        "cookies_loaded": bool(cookie_file),
        "cookie_auto_sources": {
            "YT_COOKIES": bool(os.environ.get("YT_COOKIES")),
            "YT_COOKIES_B64": bool(os.environ.get("YT_COOKIES_B64")),
            "YT_COOKIES_URL": bool(os.environ.get("YT_COOKIES_URL")),
            "YT_COOKIES_FILE": bool(os.environ.get("YT_COOKIES_FILE")),
        },
    }


@app.get("/api/cookies/status")
async def cookie_status():
    cookie_file = await prepare_cookie_file()
    return {
        "cookies_loaded": bool(cookie_file),
        "loaded_at": _COOKIE_LOADED_AT or None,
        "refresh_seconds": COOKIE_REFRESH_SECONDS,
        "uses_cookie_url": bool(os.environ.get("YT_COOKIES_URL")),
    }


@app.post("/api/cookies/reload")
async def reload_cookies(request: Request):
    api_key = os.environ.get("API_KEY")
    if api_key and request.headers.get("x-api-key") != api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    cookie_file = await prepare_cookie_file(force_refresh=True)
    return {"cookies_loaded": bool(cookie_file), "loaded_at": _COOKIE_LOADED_AT or None}


@app.get("/api/streams/{video_id}")
async def get_streams(video_id: str):
    info = await extract_info(video_id)
    formats = []
    for f in info.get("formats", []):
        if not f.get("url"):
            continue
        formats.append(
            {
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
            }
        )
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
async def get_m3u8(video_id: str):
    info = await extract_info(video_id)
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
    info = await extract_info(video_id)
    hls_urls = [
        f.get("url")
        for f in info.get("formats", [])
        if (f.get("protocol") or "").startswith("m3u8") and f.get("url")
    ]
    if not hls_urls:
        raise HTTPException(status_code=404, detail="No m3u8 streams available")
    url = hls_urls[-1]
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": DEFAULT_UA})
    return Response(content=response.content, media_type="application/vnd.apple.mpegurl")
