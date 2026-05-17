# yt-dlp API (Render対応)

YouTube動画のストリーミングURLを取得するFastAPIサービス。

## エンドポイント
- `GET /api/streams/{videoId}` — 全フォーマット直リンク
- `GET /api/streams/{videoId}/m3u8` — HLSストリーム一覧 + best
- `GET /api/streams/{videoId}/m3u8/raw` — m3u8プレイリスト本文

## Bot判定対策(Cookie不要)
以下を内蔵済み:
- `player_client` を `tv` / `ios` / `mweb` / `android_vr` / `web_safari` / `web` の順でフォールバック
- Safari の User-Agent を送信
- リトライ(`retries=3`, `extractor_retries=3`)
- `geo_bypass` 有効

これで多くの場合 "Sign in to confirm you're not a bot" を回避できます。

## どうしても通らない場合 (Render IPがブロックされた等)
環境変数 `YT_COOKIES` に Netscape形式のCookieファイルの中身をそのまま貼り付けてください。
```
# Netscape HTTP Cookie File
.youtube.com   TRUE   /   TRUE   1999999999   SID   xxxxxxxx
...
```
Renderダッシュボード → Environment → Add Environment Variable で設定。

## デプロイ
1. このフォルダをGitHubリポジトリにpush
2. Render で New → Blueprint → リポジトリ接続
3. 自動で`render.yaml`が読まれてデプロイされます

## ローカル実行
```
pip install -r requirements.txt
uvicorn main:app --reload
```
