# yt-dlp API (+ Invidious / Piped フォールバック)

YouTube動画のストリーミングURL/m3u8を取得するAPI。Renderデプロイ対応。

## エンドポイント
- `GET /api/streams/{videoId}` — 全フォーマット
- `GET /api/streams/{videoId}/m3u8` — HLS一覧 + best
- `GET /api/streams/{videoId}/m3u8/raw` — m3u8本文をプロキシ
- `GET /api/cookies/from-proxies?save=true` — Invidious/Pipedから取得したCookieを Netscape 形式で取得 (save=trueでサーバ保存)
- `GET /api/cookies/status` / `POST /api/cookies/reload`

## 取得フロー (自動フォールバック)
1. **yt-dlp** — player_client を tv/ios/mweb/android_vr/web_safari/web の順に試行
2. **Invidious** — 公開インスタンスの `/api/v1/videos/{id}` から `adaptiveFormats` `formatStreams` `hlsUrl` を取得
3. **Piped** — 公開インスタンスの `/streams/{id}` から `videoStreams` `audioStreams` `hls` を取得

→ Renderの datacenter IP が YouTube から bot 判定されても、Invidious/Piped が代理で取得してくれます。

## 環境変数 (任意)
- `INVIDIOUS_INSTANCES` — カンマ区切りで上書き (例: `https://inv.nadeko.net,https://yewtu.be`)
- `PIPED_INSTANCES` — 同上
- `YT_COOKIES` / `YT_COOKIES_B64` / `YT_COOKIES_URL` / `YT_COOKIES_FILE` — yt-dlpに渡すCookie
- `API_KEY` — `/api/cookies/reload` 保護用

## Renderデプロイ
1. このフォルダをGitHubにpush
2. Render → New → Blueprint → リポジトリ選択
3. `render.yaml` が自動で読まれデプロイされます

## ローカル
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## 注意
Invidious/Piped はあくまで公開インスタンス。落ちている / レート制限の可能性があるので
複数インスタンスがフォールバックされます。必要なら `INVIDIOUS_INSTANCES` / `PIPED_INSTANCES`
で自分の信頼するインスタンスを指定してください。

`/api/cookies/from-proxies` が返すのは各インスタンスのセッションCookieであり、
**YouTube本体のログインCookieではありません**。YouTubeの認証が必要な場合は
自分のブラウザから書き出した cookies.txt を `YT_COOKIES` 等で渡してください。
