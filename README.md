# yt-dlp API (Render デプロイ用)

YouTube 動画のストリーミングURLを取得するシンプルなAPI。

## エンドポイント

- `GET /api/streams/{videoId}` — 全フォーマットのストリーミングURL一覧
- `GET /api/streams/{videoId}/m3u8` — HLS (m3u8) URL のみ抽出 (best 付き)
- `GET /api/streams/{videoId}/m3u8/raw` — m3u8 プレイリスト本体をそのまま返す

## Render へのデプロイ

1. このフォルダを GitHub リポジトリに push
2. [Render](https://render.com) で **New +** → **Blueprint** を選び、リポジトリを接続
3. `render.yaml` が自動検出されてデプロイされます

または **New Web Service** から手動設定:
- Runtime: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

## ローカル実行

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

→ `http://localhost:8000/api/streams/dQw4w9WgXcQ`

## 注意

- YouTube側のレート制限やIPブロックを受ける可能性があります (Render無料プランは特に)
- 必要に応じて `yt_dlp` の `cookiefile` オプションなどを追加してください
