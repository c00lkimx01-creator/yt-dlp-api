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

YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "nocheckcertificate": True,
}


def extract_info(video_id: str, fmt: str | None = None) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = dict(YDL_BASE_OPTS)
    if fmt:
        opts["format"] = fmt
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def root():
    return {
        "name": "yt-dlp API",
        "endpoints": [
            "/api/streams/{video_id}",
            "/api/streams/{video_id}/m3u8",
            "/api/streams/{video_id}/m3u8/raw",
        ],
    }


@app.get("/api/streams/{video_id}")
def get_streams(video_id: str):
    """全フォーマットの直リンク情報を返す"""
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
    """HLS (m3u8) フォーマットのURLを返す"""
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
        if f.get("protocol", "").startswith("m3u8") or (f.get("url") or "").endswith(".m3u8") or "m3u8" in (f.get("url") or "")
    ]
    if not hls:
        raise HTTPException(status_code=404, detail="No m3u8 streams available")
    # ベスト(最高画質)を先頭に
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
    """m3u8 プレイリストの中身そのものを返す"""
    info = extract_info(video_id)
    hls_urls = [
        f.get("url")
        for f in info.get("formats", [])
        if f.get("protocol", "").startswith("m3u8") and f.get("url")
    ]
    if not hls_urls:
        raise HTTPException(status_code=404, detail="No m3u8 streams available")
    url = hls_urls[-1]  # 通常マスター/最高品質
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
    return Response(content=r.content, media_type="application/vnd.apple.mpegurl")
