import asyncio
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
# 概要
# ---------------------------------------------------------------------------
# 1. yt-dlp の player_client フォールバックを試す
# 2. ダメなら Invidious / Piped の公開インスタンスからストリーム URL を取得
#    (= 実質的に Invidious/Piped を「Cookie / bot 回避代理」として利用)
# 3. さらに Cookie を環境変数や URL から自動ロードして yt-dlp に渡す機能も保持
# ---------------------------------------------------------------------------

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)

PLAYER_CLIENT_FALLBACKS = [
    ["tv", "ios", "mweb"],
    ["ios", "mweb"],
    ["android_vr"],
    ["web_safari"],
    ["web"],
]

# ---------------------------------------------------------------------------
# Invidious / Piped インスタンス
# ---------------------------------------------------------------------------
# 環境変数 INVIDIOUS_INSTANCES / PIPED_INSTANCES に
# カンマ区切りで上書き可能 (例: "https://inv.nadeko.net,https://invidious.fdn.fr")
DEFAULT_INVIDIOUS = [
    "https://invidious.nerdvpn.de",
    "https://inv.nadeko.net",
    "https://invidious.privacyredirect.com",
    "https://invidious.lunar.icu",
    "https://invidious.f5.si",
    "https://yewtu.be",
    "https://invidious.materialio.us",
    "https://invidious.reallyaweso.me",
    "https://iv.duti.dev",
    "https://invidious.fdn.fr",
]
DEFAULT_PIPED = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.r4fo.com",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.leptons.xyz",
    "https://api.piped.private.coffee",
    "https://pipedapi.drgns.space",
    "https://pipedapi.ducks.party",
]


def _instances(env_key: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_key)
    if raw:
        return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    return [u.rstrip("/") for u in default]


# ---------------------------------------------------------------------------
# Cookie 自動ロード
# ---------------------------------------------------------------------------
COOKIE_REFRESH_SECONDS = int(os.environ.get("YT_COOKIES_REFRESH_SECONDS", "1800"))
_COOKIE_FILE: str | None = None
_COOKIE_SOURCE_SIGNATURE: str | None = None
_COOKIE_LOADED_AT = 0.0


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


# ---------------------------------------------------------------------------
# yt-dlp 抽出
# ---------------------------------------------------------------------------
def _base_opts(player_clients: list[str], cookie_file: str | None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "retries": 2,
        "extractor_retries": 2,
        "socket_timeout": 15,
        "http_headers": {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {"youtube": {"player_client": player_clients}},
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


def _ytdlp_extract(video_id: str, cookie_file: str | None) -> dict | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    for clients in PLAYER_CLIENT_FALLBACKS:
        try:
            with yt_dlp.YoutubeDL(_base_opts(clients, cookie_file)) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Invidious フォールバック
# ---------------------------------------------------------------------------
async def _fetch_invidious(video_id: str) -> dict | None:
    instances = _instances("INVIDIOUS_INSTANCES", DEFAULT_INVIDIOUS)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                 headers={"User-Agent": DEFAULT_UA}) as client:
        for base in instances:
            try:
                r = await client.get(f"{base}/api/v1/videos/{video_id}")
                if r.status_code != 200:
                    continue
                data = r.json()
                if data.get("error"):
                    continue
                formats = []
                for f in data.get("adaptiveFormats", []) + data.get("formatStreams", []):
                    u = f.get("url")
                    if not u:
                        continue
                    formats.append({
                        "format_id": str(f.get("itag", "")),
                        "ext": f.get("container") or (f.get("type") or "").split("/")[-1].split(";")[0],
                        "protocol": "https",
                        "resolution": f.get("resolution"),
                        "width": None,
                        "height": int(f["resolution"].rstrip("p")) if f.get("resolution", "").endswith("p") else None,
                        "fps": f.get("fps"),
                        "vcodec": f.get("encoding") if "video" in (f.get("type") or "") else "none",
                        "acodec": f.get("encoding") if "audio" in (f.get("type") or "") else None,
                        "tbr": int(f["bitrate"]) / 1000 if f.get("bitrate") else None,
                        "filesize": None,
                        "url": u,
                    })
                hls = data.get("hlsUrl")
                if hls:
                    formats.append({
                        "format_id": "hls",
                        "ext": "m3u8", "protocol": "m3u8_native",
                        "resolution": None, "width": None, "height": None, "fps": None,
                        "vcodec": None, "acodec": None, "tbr": None, "filesize": None,
                        "url": hls,
                    })
                return {
                    "id": video_id,
                    "title": data.get("title"),
                    "duration": data.get("lengthSeconds"),
                    "thumbnail": (data.get("videoThumbnails") or [{}])[0].get("url"),
                    "uploader": data.get("author"),
                    "is_live": data.get("liveNow", False),
                    "formats": formats,
                    "_source": f"invidious:{base}",
                }
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Piped フォールバック
# ---------------------------------------------------------------------------
async def _fetch_piped(video_id: str) -> dict | None:
    instances = _instances("PIPED_INSTANCES", DEFAULT_PIPED)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                 headers={"User-Agent": DEFAULT_UA}) as client:
        for base in instances:
            try:
                r = await client.get(f"{base}/streams/{video_id}")
                if r.status_code != 200:
                    continue
                data = r.json()
                if data.get("error"):
                    continue
                formats = []
                for f in data.get("videoStreams", []):
                    if not f.get("url"):
                        continue
                    formats.append({
                        "format_id": f.get("itag") and str(f["itag"]) or f.get("quality"),
                        "ext": f.get("format", "").lower().replace("_", "") or None,
                        "protocol": "https",
                        "resolution": f.get("quality"),
                        "width": f.get("width"), "height": f.get("height"),
                        "fps": f.get("fps"),
                        "vcodec": f.get("codec"),
                        "acodec": "none" if f.get("videoOnly") else None,
                        "tbr": (f.get("bitrate") or 0) / 1000 or None,
                        "filesize": f.get("contentLength"),
                        "url": f["url"],
                    })
                for f in data.get("audioStreams", []):
                    if not f.get("url"):
                        continue
                    formats.append({
                        "format_id": f.get("itag") and str(f["itag"]) or f.get("quality"),
                        "ext": f.get("format", "").lower().replace("_", "") or None,
                        "protocol": "https",
                        "resolution": None, "width": None, "height": None, "fps": None,
                        "vcodec": "none", "acodec": f.get("codec"),
                        "tbr": (f.get("bitrate") or 0) / 1000 or None,
                        "filesize": f.get("contentLength"),
                        "url": f["url"],
                    })
                hls = data.get("hls")
                if hls:
                    formats.append({
                        "format_id": "hls",
                        "ext": "m3u8", "protocol": "m3u8_native",
                        "resolution": None, "width": None, "height": None, "fps": None,
                        "vcodec": None, "acodec": None, "tbr": None, "filesize": None,
                        "url": hls,
                    })
                return {
                    "id": video_id,
                    "title": data.get("title"),
                    "duration": data.get("duration"),
                    "thumbnail": data.get("thumbnailUrl"),
                    "uploader": data.get("uploader"),
                    "is_live": data.get("livestream", False),
                    "formats": formats,
                    "_source": f"piped:{base}",
                }
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Cookie 取得: Invidious / Piped はログイン Cookie を提供していないため、
# ここでは「インスタンスからアクセスして得られた Set-Cookie」をそのまま
# Netscape 形式に書き出し YT_COOKIES_FILE 相当として利用するモードを用意。
# あくまで補助的: YouTube 本体の認証 Cookie ではない点に注意。
# ---------------------------------------------------------------------------
async def fetch_cookies_from_proxies() -> str:
    """Invidious / Piped のインスタンスを叩いて取得した Cookie を
    Netscape 形式で返す (デモ・実験用)。"""
    lines = ["# Netscape HTTP Cookie File"]
    targets = (
        [(b, f"{b}/watch?v=dQw4w9WgXcQ") for b in _instances("INVIDIOUS_INSTANCES", DEFAULT_INVIDIOUS)[:3]]
        + [(b, f"{b}/streams/dQw4w9WgXcQ") for b in _instances("PIPED_INSTANCES", DEFAULT_PIPED)[:3]]
    )
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False,
                                 headers={"User-Agent": DEFAULT_UA}) as client:
        for base, url in targets:
            try:
                r = await client.get(url)
                host = base.split("://", 1)[-1].split("/", 1)[0]
                for k, v in r.cookies.items():
                    # Netscape: domain TAB include_sub TAB path TAB secure TAB expiry TAB name TAB value
                    lines.append(f".{host}\tTRUE\t/\tTRUE\t0\t{k}\t{v}")
            except Exception:
                continue
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 共通抽出 (yt-dlp → Invidious → Piped の順)
# ---------------------------------------------------------------------------
async def extract_info(video_id: str) -> dict:
    cookie_file = await prepare_cookie_file()
    info = await asyncio.to_thread(_ytdlp_extract, video_id, cookie_file)
    if info and info.get("formats"):
        info.setdefault("_source", "yt-dlp")
        return info

    inv = await _fetch_invidious(video_id)
    if inv and inv.get("formats"):
        return inv

    pip = await _fetch_piped(video_id)
    if pip and pip.get("formats"):
        return pip

    raise HTTPException(
        status_code=429,
        detail="yt-dlp / Invidious / Piped すべて失敗しました。時間を置くか Cookie を設定してください。",
    )


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "name": "yt-dlp API (+Invidious/Piped fallback)",
        "endpoints": [
            "/api/streams/{video_id}",
            "/api/streams/{video_id}/m3u8",
            "/api/streams/{video_id}/m3u8/raw",
            "/api/cookies/status",
            "/api/cookies/reload",
            "/api/cookies/from-proxies",
        ],
        "invidious_instances": _instances("INVIDIOUS_INSTANCES", DEFAULT_INVIDIOUS),
        "piped_instances": _instances("PIPED_INSTANCES", DEFAULT_PIPED),
    }


@app.get("/api/cookies/status")
async def cookie_status():
    cookie_file = await prepare_cookie_file()
    return {
        "cookies_loaded": bool(cookie_file),
        "loaded_at": _COOKIE_LOADED_AT or None,
        "refresh_seconds": COOKIE_REFRESH_SECONDS,
    }


@app.post("/api/cookies/reload")
async def reload_cookies(request: Request):
    api_key = os.environ.get("API_KEY")
    if api_key and request.headers.get("x-api-key") != api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    cookie_file = await prepare_cookie_file(force_refresh=True)
    return {"cookies_loaded": bool(cookie_file), "loaded_at": _COOKIE_LOADED_AT or None}


@app.get("/api/cookies/from-proxies")
async def cookies_from_proxies(save: bool = False):
    """Invidious / Piped から Set-Cookie を集めて Netscape 形式で返す。
    save=true でサーバ側 cookie ファイルとしても保存し yt-dlp に使う。"""
    text = await fetch_cookies_from_proxies()
    saved = None
    if save:
        saved = _write_cookie_file(text)
    return Response(
        content=text,
        media_type="text/plain",
        headers={"x-saved-path": saved or ""},
    )


@app.get("/api/streams/{video_id}")
async def get_streams(video_id: str):
    info = await extract_info(video_id)
    formats = [
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
        for f in info.get("formats", [])
        if f.get("url")
    ]
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "is_live": info.get("is_live", False),
        "source": info.get("_source"),
        "formats": formats,
    }


def _pick_m3u8(info: dict) -> list[dict]:
    return [
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
        if f.get("url") and (
            (f.get("protocol") or "").startswith("m3u8")
            or ".m3u8" in (f.get("url") or "")
        )
    ]


@app.get("/api/streams/{video_id}/m3u8")
async def get_m3u8(video_id: str):
    info = await extract_info(video_id)
    hls = _pick_m3u8(info)
    if not hls:
        raise HTTPException(status_code=404, detail="No m3u8 streams available")
    hls.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0), reverse=True)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "is_live": info.get("is_live", False),
        "source": info.get("_source"),
        "best": hls[0],
        "streams": hls,
    }


@app.get("/api/streams/{video_id}/m3u8/raw")
async def get_m3u8_raw(video_id: str):
    info = await extract_info(video_id)
    hls = _pick_m3u8(info)
    if not hls:
        raise HTTPException(status_code=404, detail="No m3u8 streams available")
    url = hls[0]["url"]
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": DEFAULT_UA})
    return Response(content=response.content, media_type="application/vnd.apple.mpegurl")
