#!/usr/bin/env python3
"""東証グロース市場250指数(グロース250)の大引け概況を生成し、
WordPressに本物の記事として投稿 + Xにネイティブ投稿(リンクなし)する。

GitHub Actions の cron から平日の引け後に実行される想定。

データ戦略:
  - 値上がり/値下がりの主役銘柄・騰落数(breadth) … 自前の growth250-data.json
    (moo-growth250-tracker が yfinance で生成しロリポップに配信。確定値)
  - 指数の終値・前日比・当日の材料 … Claude のウェブ検索
    (Anthropic 側で実行されるため、Actions IP のブロックを受けない。
     引け後に走らせれば日経の大引け記事等から終値・前日比・騰落率を取得できる)

処理の流れ:
  1. growth250-data.json を取得し、主役銘柄と騰落数を抽出
  2. それを材料として Claude に渡し、ウェブ検索で指数の数字と背景を調べさせて
     「ブログ記事(タイトル+本文)」と「ツイート(140字・リンクなし)」を一括生成
  3. WordPress REST API で記事を公開
  4. 同じ概況を X にネイティブ投稿(リンクなし)

環境変数(GitHub Secrets 経由):
  ANTHROPIC_API_KEY, WP_BASE_URL, WP_USER, WP_APP_PASSWORD, WP_CATEGORY_ID(任意),
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET,
  DRY_RUN(値が入っていれば投稿せず生成結果を表示するだけ)
"""

import os
import re
import sys
import json
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import anthropic
import tweepy

JST = ZoneInfo("Asia/Tokyo")

# moo-growth250-tracker がロリポップに配信している構成銘柄データ
JSON_URL = "https://moo-stock-blog.com/growth250-data.json"

MODEL = "claude-haiku-4-5-20251001"           # 本文生成(軽量モデル)
WEB_SEARCH_TOOL = "web_search_20260209"       # ※現行版。docsで最新を要確認
TWEET_LIMIT = 140                             # 全角換算のおおよその上限(無印アカウント)


# --------------------------------------------------------------------------
# 1. 自前JSONから主役銘柄・騰落数を取得
# --------------------------------------------------------------------------
def fetch_market_data() -> dict | None:
    """growth250-data.json を読み、主役銘柄とbreadthを返す。失敗時 None。"""
    try:
        r = requests.get(JSON_URL, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[warn] JSON取得失敗: {e}", file=sys.stderr)
        return None

    rows = data.get("all", [])
    up = sum(1 for x in rows if x.get("change_pct", 0) > 0)
    down = sum(1 for x in rows if x.get("change_pct", 0) < 0)
    flat = sum(1 for x in rows if x.get("change_pct", 0) == 0)

    return {
        "updated_at": data.get("updated_at", ""),
        "total": data.get("total_count", len(rows)),
        "up": up,
        "down": down,
        "flat": flat,
        "best": data.get("best", [])[:5],
        "worst": data.get("worst", [])[:5],
    }


def is_data_today(updated_at: str) -> bool:
    """updated_at('2026-05-28 15:32 JST')の日付が本日(JST)かどうか。"""
    try:
        day = updated_at.split()[0]  # 'YYYY-MM-DD'
        return day == dt.datetime.now(JST).strftime("%Y-%m-%d")
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


def _fmt_movers(items) -> str:
    return "、".join(
        f"{x.get('name_jp', '')}({x.get('change_pct', 0):+.1f}%)" for x in items
    ) or "(該当なし)"


def generate_content(m: dict) -> dict:
    """{'title','body_html','tweet'} を返す。"""
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を自動参照
    today_label = dt.datetime.now(JST).strftime("%-m/%-d")

    prompt = f"""あなたは日本株の市況コメントを書く新聞記者です。

本日({today_label})の東証グロース市場250指数(グロース250)の概況を書きます。
以下は当日の構成銘柄データ(自前集計・正確)です。**個別銘柄や騰落数の言及にはこの値を使ってください**:
  値上がり {m['up']}銘柄 / 値下がり {m['down']}銘柄 / 変わらず {m['flat']}銘柄(全{m['total']}銘柄)
  上昇上位: {_fmt_movers(m['best'])}
  下落上位: {_fmt_movers(m['worst'])}

次に、**ウェブ検索で本日のグロース250の大引け情報を調べてください**:
  - 日経「新興株◯日…」や株探・JPX等の信頼できる情報源から、
    指数の終値(例 835.56)・前日比(◯.◯ポイント)・騰落率(◯.◯%)・
    方向(反発/続伸/反落/続落)を取得する。
  - 値動きの背景(全体地合い、日経平均、テーマ等の材料)も調べる。
  - **数字は検索で確認できたものだけ**を書くこと。確認できなければ、指数の正確な数値は
    書かず、上の騰落数・個別銘柄から方向感だけを述べる(数字を創作しない)。

**重要(正確性):**
  - 騰落数は正しく表現する。値下がり銘柄({m['down']})が値上がり銘柄({m['up']})より多い場合は
    「値上がり優勢」と書かない(この場合はむしろ値下がり銘柄が優勢)。
  - 指数の方向(検索結果)と騰落数は食い違うことがある(指数は時価総額加重のため、値下がり
    銘柄が多くても主力大型株が上げれば指数は上昇しうる)。食い違う場合は両方を正確に述べ、
    矛盾した表現をしないこと。
  - 当日の終値・前日比・騰落率を検索で確認できたら、**必ず見出しと本文の冒頭に入れる**こと。

その上で、次の3つを**JSONだけ**で出力(コードフェンス・前置き・説明は一切不要):
{{
  "title": "SEOを意識した記事見出し。日付・終値や方向を含め30〜45字程度。",
  "body_html": "<p>段落</p> を2〜3個。最初の段落で指数の結果、次に背景、必要なら主な個別銘柄。事実ベースで簡潔に。タグは<p>と<strong>程度のみ。",
  "tweet": "ツイート本文。全角{TWEET_LIMIT}字以内。**リンクは入れない**。末尾に #グロース250 #新興市場 を付けてよい。"
}}
JSON以外は出力しないこと。"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        tools=[{
            "type": WEB_SEARCH_TOOL,
            "name": "web_search",
            "max_uses": 5,
            "allowed_callers": ["direct"],
        }],
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
    m = fetch_market_data()
    if m is None:
        print("[skip] 構成銘柄データを取得できませんでした。終了します。")
        return 0

    if not is_data_today(m["updated_at"]):
        print(f"[skip] JSONの更新日({m['updated_at']})が本日ではありません。"
              f"休場日 or 更新待ちとみなしスキップします。")
        return 0

    print(f"[data] 値上がり{m['up']}/値下がり{m['down']}/変わらず{m['flat']}"
          f"(全{m['total']}) updated_at={m['updated_at']}")

    content = generate_content(m)
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
    try:
        link = post_to_wordpress(title, body)
        print(f"[wordpress] 公開しました: {link}")
    except Exception as e:
        ok = False
        print(f"[error] WordPress投稿に失敗: {e}", file=sys.stderr)

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
