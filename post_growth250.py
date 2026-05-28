#!/usr/bin/env python3
"""東証グロース市場250指数(グロース250)の大引け概況を生成し、
WordPressに本物の記事として投稿 + Xにネイティブ投稿(リンクなし)する。

GitHub Actions の cron から平日の引け後に実行される想定。

処理の流れ:
  1. kabutan の日足四本値ページから本日の確定終値・前日比をスクレイピング
  2. その数字 + 当日の材料(Claude のウェブ検索ツールで自動取得)から、
     「ブログ記事(タイトル+本文)」と「ツイート本文(140字・リンクなし)」を
     1回の生成でまとめて作成
  3. WordPress REST API で記事を公開(=SEOに効く本物の投稿)
  4. 同じ概況をXにネイティブ投稿(リンクは入れない)

ブログとXは独立して投稿し、片方が失敗してももう片方は実行します。

環境変数(GitHub Secrets 経由):
  ANTHROPIC_API_KEY  : Anthropic API キー
  WP_BASE_URL        : WordPressのURL (例: https://moo-stock-blog.com)
  WP_USER            : 投稿者ユーザー名
  WP_APP_PASSWORD    : WordPressのアプリケーションパスワード
  WP_CATEGORY_ID     : (任意) 投稿先カテゴリID
  X_API_KEY          : X(Twitter) API Key (Consumer Key)
  X_API_SECRET       : X API Key Secret (Consumer Secret)
  X_ACCESS_TOKEN     : Access Token
  X_ACCESS_SECRET    : Access Token Secret
  DRY_RUN            : 値が入っていれば投稿せず生成結果を表示するだけ(テスト用)
"""

import os
import re
import sys
import json
import datetime as dt
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
import anthropic
import tweepy

JST = ZoneInfo("Asia/Tokyo")

# 東証グロース市場250指数 = kabutan コード 0012
KABUTAN_URL = "https://kabutan.jp/stock/kabuka?code=0012"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 growth250-bot"
    )
}

MODEL = "claude-haiku-4-5-20251001"           # 本文生成(軽量モデル)
WEB_SEARCH_TOOL = "web_search_20260209"       # ※現行版。docsで最新を要確認
TWEET_LIMIT = 140                             # 全角換算のおおよその上限(無印アカウント)


# --------------------------------------------------------------------------
# 1. 終値データの取得(スクレイピング)
# --------------------------------------------------------------------------
def _to_float(s: str) -> float:
    return float(s.replace(",", "").replace("＋", "+").replace("△", "-").strip())


def _find_ohlc_tables(soup: BeautifulSoup):
    """ヘッダに『始値』『終値』『前日比』を含むテーブルを順に返す。"""
    tables = []
    for table in soup.find_all("table"):
        head = table.get_text()
        if "始値" in head and "終値" in head and "前日比" in head:
            tables.append(table)
    return tables


def _parse_row(cells):
    """データ行(先頭セルが yy/mm/dd)だけを辞書化。それ以外は None。"""
    texts = [c.get_text(strip=True) for c in cells]
    if len(texts) < 7:
        return None
    if not re.match(r"\d{2}/\d{2}/\d{2}", texts[0]):
        return None
    try:
        return {
            "date_raw": texts[0],
            "close": _to_float(texts[4]),
            "change": _to_float(texts[5]),
            "pct": _to_float(texts[6]),
        }
    except ValueError:
        return None


def fetch_close():
    """本日の四本値と前日の騰落方向を取得。失敗時 None。"""
    resp = requests.get(KABUTAN_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    ohlc_tables = _find_ohlc_tables(soup)
    if not ohlc_tables:
        print("[warn] OHLCテーブルが見つかりません。kabutanのHTML構造変更の可能性。",
              file=sys.stderr)
        return None

    today_row = None
    for tr in ohlc_tables[0].find_all("tr"):
        parsed = _parse_row(tr.find_all(["td", "th"]))
        if parsed:
            today_row = parsed
            break
    if today_row is None:
        print("[warn] 本日行の解析に失敗しました。", file=sys.stderr)
        return None

    prev_dir = None
    if len(ohlc_tables) >= 2:
        for tr in ohlc_tables[1].find_all("tr"):
            parsed = _parse_row(tr.find_all(["td", "th"]))
            if parsed:
                prev_dir = "up" if parsed["change"] >= 0 else "down"
                break

    today_row["prev_dir"] = prev_dir
    return today_row


def is_today_jst(date_raw: str) -> bool:
    today = dt.datetime.now(JST).date()
    try:
        yy, mm, dd = (int(x) for x in date_raw.split("/"))
        return dt.date(2000 + yy, mm, dd) == today
    except Exception:
        return False


def weighted_len(text: str) -> int:
    """Xの重み付き文字数(ざっくり): ASCIIは1、それ以外(全角等)は2。"""
    return sum(1 if ord(ch) < 0x1100 else 2 for ch in text)


# --------------------------------------------------------------------------
# 2. 概況の生成(Claude + ウェブ検索) — ブログ用とツイート用を一括生成
# --------------------------------------------------------------------------
def _extract_text(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def _parse_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j != -1:
            t = t[i:j + 1]
    return json.loads(t)


def generate_content(data) -> dict:
    """{'title','body_html','tweet'} を返す。"""
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を自動参照
    today_label = dt.datetime.now(JST).strftime("%-m/%-d")
    prev_hint = {
        "up": "前営業日は上昇(今日も上昇なら『続伸』、下落なら『反落』)。",
        "down": "前営業日は下落(今日上昇なら『反発』、下落なら『続落』)。",
        None: "前日方向は不明なので方向の語(反発等)は断定しすぎない。",
    }[data.get("prev_dir")]

    prompt = f"""あなたは日本株の市況コメントを書く新聞記者です。

本日({today_label})の東証グロース市場250指数(グロース250)の確定値は以下です。
推測で変えず、この数値を**そのまま**使ってください:
  終値: {data['close']:.2f}
  前日比: {data['change']:+.2f}ポイント({data['pct']:+.2f}%)
{prev_hint}

ウェブ検索で、本日のグロース市場の値動きの背景を調べてください:
  - 物色の材料(全体地合い、日経平均の動き、テーマ等)
  - 上昇/下落が目立った主な個別銘柄

その上で、次の3つを**JSONだけ**で出力してください
(コードフェンスや前置き・説明は一切不要。純粋なJSONのみ):
{{
  "title": "SEOを意識した記事見出し。日付・終値・方向(反発等)を含め、30〜45字程度。",
  "body_html": "<p>段落</p> を2〜3個。最初の段落で指数の結果(終値と前日比)、次に背景、必要なら主な個別銘柄。事実ベースで簡潔に。HTMLタグは<p>と<strong>程度のみ。",
  "tweet": "ツイート本文。全角{TWEET_LIMIT}字以内。**リンクは入れない**。末尾に #グロース250 #新興市場 を付けてよい。"
}}
数値は上記の正確値を使い、JSON以外は出力しないこと。"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        tools=[{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": prompt}],
    )
    obj = _parse_json(_extract_text(resp))

    # ツイートが長すぎる場合は1回だけ短縮
    if weighted_len(obj.get("tweet", "")) > TWEET_LIMIT * 2:
        resp2 = client.messages.create(
            model=MODEL,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": f"次のツイートを、意味を保ったまま全角{TWEET_LIMIT}字以内に。"
                           f"リンクは入れない。本文のみ出力:\n\n{obj['tweet']}",
            }],
        )
        obj["tweet"] = _extract_text(resp2)
    return obj


# --------------------------------------------------------------------------
# 3a. WordPress に本物の記事として投稿
# --------------------------------------------------------------------------
def post_to_wordpress(title: str, body_html: str) -> str:
    base = os.environ["WP_BASE_URL"].rstrip("/")
    url = f"{base}/wp-json/wp/v2/posts"
    payload = {"title": title, "content": body_html, "status": "publish"}
    cat = os.environ.get("WP_CATEGORY_ID")
    if cat:
        payload["categories"] = [int(cat)]
    r = requests.post(
        url,
        json=payload,
        auth=(os.environ["WP_USER"], os.environ["WP_APP_PASSWORD"]),
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("link", "(URL不明)")


# --------------------------------------------------------------------------
# 3b. X へネイティブ投稿(リンクなし)
# --------------------------------------------------------------------------
def post_to_x(text: str):
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )
    return client.create_tweet(text=text)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    data = fetch_close()
    if data is None:
        print("[skip] 終値データを取得できませんでした。終了します。")
        return 0

    if not is_today_jst(data["date_raw"]):
        print(f"[skip] 最新データの日付({data['date_raw']})が本日ではありません。"
              f"休場日とみなしスキップします。")
        return 0

    print(f"[data] 終値={data['close']:.2f} 前日比={data['change']:+.2f}"
          f"({data['pct']:+.2f}%) 前日方向={data.get('prev_dir')}")

    content = generate_content(data)
    title = content.get("title", "").strip()
    body = content.get("body_html", "").strip()
    tweet = content.get("tweet", "").strip()

    print("\n===== ブログ記事 =====")
    print("TITLE:", title)
    print("BODY :", body)
    print(f"\n===== ツイート(重み付き{weighted_len(tweet)}/上限{TWEET_LIMIT*2}) =====")
    print(tweet, "\n")

    if os.environ.get("DRY_RUN"):
        print("[dry-run] DRY_RUN のため投稿しません。")
        return 0

    ok = True

    # ① ブログ(真の置き場)を先に
    try:
        link = post_to_wordpress(title, body)
        print(f"[wordpress] 公開しました: {link}")
    except Exception as e:
        ok = False
        print(f"[error] WordPress投稿に失敗: {e}", file=sys.stderr)

    # ② X(拡散役・リンクなし)
    try:
        resp = post_to_x(tweet)
        tid = resp.data.get("id") if resp and resp.data else "?"
        print(f"[x] 投稿しました: tweet id = {tid}")
    except Exception as e:
        ok = False
        print(f"[error] X投稿に失敗: {e}", file=sys.stderr)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
