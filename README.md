# yt-dlp API (Render対応)

YouTube動画のストリーミングURL / m3u8 URLを取得するFastAPIサービスです。

## エンドポイント

- `GET /api/streams/{videoId}` — 全フォーマット直リンク
- `GET /api/streams/{videoId}/m3u8` — HLSストリーム一覧 + best
- `GET /api/streams/{videoId}/m3u8/raw` — m3u8プレイリスト本文
- `GET /api/cookies/status` — Cookie読み込み状態
- `POST /api/cookies/reload` — Cookie URL/ファイルを強制再読み込み

## 重要: bot判定はCookieなしで完全自動回避できません

RenderなどのサーバーIPはYouTubeからbot判定されやすく、次のエラーが出る場合があります。

```text
Sign in to confirm you’re not a bot
```

この場合、サーバー側がログインCookieを勝手に取得することはできません。自分のブラウザからエクスポートしたCookieをRenderの環境変数に設定してください。

## Cookie自動読み込み方法

以下のどれか1つをRenderの Environment に設定してください。

### 方法1: `YT_COOKIES` にそのまま貼る

Netscape形式Cookieをそのまま貼り付けます。

```text
# Netscape HTTP Cookie File
.youtube.com   TRUE   /   TRUE   1999999999   SID   xxxxxxxx
...
```

### 方法2: `YT_COOKIES_B64` にBase64で貼る

改行が壊れる場合におすすめです。

```bash
base64 -i cookies.txt | pbcopy
```

Renderの `YT_COOKIES_B64` に貼り付けます。

### 方法3: `YT_COOKIES_URL` から自動取得する

自分で管理するプライベートURLに `cookies.txt` を置き、Renderに設定します。

```text
YT_COOKIES_URL=https://example.com/private/cookies.txt
```

Bearer認証付きURLにする場合:

```text
YT_COOKIES_URL=https://example.com/private/cookies.txt
YT_COOKIES_URL_TOKEN=xxxxx
```

このAPIは起動時/一定間隔でCookieを自動取得します。更新間隔はデフォルト30分です。

```text
YT_COOKIES_REFRESH_SECONDS=1800
```

強制再読み込み:

```bash
curl -X POST https://###.onrender.com/api/cookies/reload
```

`API_KEY` を設定している場合:

```bash
curl -X POST https://###.onrender.com/api/cookies/reload -H 'x-api-key: YOUR_API_KEY'
```

### 方法4: `YT_COOKIES_FILE` を指定する

Dockerや有料環境などでファイル配置できる場合:

```text
YT_COOKIES_FILE=/etc/secrets/cookies.txt
```

## Cookieの書き出し

ブラウザ拡張の「Get cookies.txt LOCALLY」などでYouTubeのCookieをNetscape形式でエクスポートしてください。

注意:
- Cookieはログイン情報なので絶対に公開リポジトリへ入れない
- RenderのEnvironment Variablesにだけ保存する
- Cookieが期限切れになったら再エクスポートして更新する
- `YT_COOKIES_URL` を使う場合、URLは必ず非公開/認証付きにする

## 内蔵済みの補助対策

Cookieがない場合でも、以下を自動で試します。

- `player_client` を `tv` / `ios` / `mweb` / `android_vr` / `web_safari` / `web` の順でフォールバック
- Safari User-Agent
- `geo_bypass`
- retry設定

ただし、bot判定が強いIPではCookieが必須です。

## デプロイ

1. このフォルダをGitHubリポジトリにpush
2. Renderで New → Blueprint → リポジトリ接続
3. 自動で `render.yaml` が読まれてデプロイされます
4. 必要ならRenderの Environment に `YT_COOKIES_B64` または `YT_COOKIES_URL` を設定

## ローカル実行

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```
