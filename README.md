# グロース250 大引け概況 自動投稿ボット

東証グロース市場250指数の引け値をもとに当日の市況概況を生成し、
**WordPressに本物の記事として公開** + **Xにネイティブ投稿(リンクなし)** する
GitHub Actions ボットです。

- **スケジューラ**: GitHub Actions の cron(平日15:50 JST 目安)
- **データ**: kabutan の日足四本値をスクレイピング(正確な終値・前日比)
- **本文**: `claude-haiku-4-5-20251001` がウェブ検索で当日の材料を拾い、
  「ブログ記事(タイトル+本文)」と「ツイート本文(140字・リンクなし)」を一括生成
- **配信**:
  - ブログ = WordPress REST API で公開(=検索に効く本物の投稿。広告ブロッカーの影響を受けない)
  - X = 同じ概況をフル本文でネイティブ投稿。**リンクは入れない**(リンク付きはXで拡散が落ちるため)
- ブログとXは独立投稿。片方が失敗してももう片方は実行します。

---

## ファイル構成

```
growth250-bot/
├── post_growth250.py
├── requirements.txt
├── README.md
└── .github/workflows/growth250.yml
```

## セットアップ

### 1. リポジトリに置く
この `growth250-bot/` の中身を GitHub リポジトリの**ルート**に置きます
(`.github/workflows/growth250.yml` の位置を保つこと)。

### 2. WordPress アプリケーションパスワードを発行
WordPress 管理画面 → **ユーザー → プロフィール → アプリケーションパスワード** で
新規発行(`xxxx xxxx xxxx xxxx xxxx xxxx` 形式)。投稿権限のあるユーザーで作成してください。

> **ロリポップ等の共有サーバーでの注意**: Apache/CGI 環境では `Authorization` ヘッダが
> 剥がされ、REST APIの認証が 401 になることがあります。その場合は `.htaccess` に
> 次を追加してください:
> ```
> SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1
> ```

### 3. Secrets を登録
リポジトリの **Settings → Secrets and variables → Actions** で以下を登録:

| 名前 | 中身 |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API キー |
| `WP_BASE_URL` | 例: `https://moo-stock-blog.com` |
| `WP_USER` | WordPress 投稿者ユーザー名 |
| `WP_APP_PASSWORD` | 上で発行したアプリケーションパスワード |
| `WP_CATEGORY_ID` | (任意) 投稿先カテゴリID。無ければ未登録でOK |
| `X_API_KEY` | X の API Key (Consumer Key) |
| `X_API_SECRET` | X の API Key Secret |
| `X_ACCESS_TOKEN` | Access Token(**Read and Write** 権限) |
| `X_ACCESS_SECRET` | Access Token Secret |

### 4. Anthropic 側でウェブ検索を有効化
本文生成でウェブ検索ツールを使うため、Anthropic Console の組織設定で
**Web search を有効化**しておく必要があります(課金は $10 / 1,000 検索)。

---

## まず手動でテスト

**Actions タブ → このワークフロー → Run workflow** で手動実行できます。
`dry_run` を **true** のまま実行すると、投稿せずに「ブログ記事(タイトル・本文)」と
「ツイート本文」がログに出るだけなので、内容・数字・文字数を確認できます。
問題なければ `dry_run=false` で実投稿、その後はcronに任せます。

---

## 注意点・既知の弱点

- **スクレイピングは壊れやすい**: kabutan の HTML 構造が変わると `fetch_close()` の
  パース(`_find_ohlc_tables` / `_parse_row`)が動かなくなります。投稿が止まったら
  まずここを確認・修正してください。利用規約・robots も適宜ご確認を。
  (将来、自前の `growth250-data.json` に終値が入るなら、そちら参照に替える方が堅牢です)
- **cron の遅延**: GitHub Actions の cron は混雑時に数分〜十数分ずれます。早すぎると終値が
  未確定なことがあるため `50 6 * * 1-5`(15:50 JST)に余裕をもたせています。
- **祝日・休場**: 取得した最新データの日付が「本日(JST)」でなければ自動でスキップします。
- **WordPress 認証 401**: 上記の `.htaccess` 対応(Authorization ヘッダ)を参照。
- **ウェブ検索ツールのバージョン**: `WEB_SEARCH_TOOL = "web_search_20260209"` は本ファイル
  作成時点の現行版です。エラーが出たら最新の type 文字列を docs で確認してください。
- **モデル変更**: 文章の質を上げたいなら `MODEL` を `claude-sonnet-4-6` 等に変更可(コスト増)。
- **時刻の扱い**: cron は UTC、日付判定は JST(`Asia/Tokyo`)で分離済み。
  ("時刻ズレ対策" の教訓どおり、JST/UTC を混同しない設計)

## ローカルでの確認

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
export WP_BASE_URL=https://moo-stock-blog.com  WP_USER=...  WP_APP_PASSWORD=...
export X_API_KEY=...  X_API_SECRET=...  X_ACCESS_TOKEN=...  X_ACCESS_SECRET=...
export DRY_RUN=1          # 投稿せず生成結果だけ表示
python post_growth250.py
```
