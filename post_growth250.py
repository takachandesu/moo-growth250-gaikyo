#!/usr/bin/env python3
"""東証グロース市場250指数(グロース250)の大引け概況を生成し、
WordPressに本物の記事として投稿 + Xにネイティブ投稿(リンクなし)する。

GitHub Actions の cron から平日の引け後に実行される想定。

データ戦略:
  - 概況は「自前の growth250-data.json」の値だけを根拠に生成する
    (moo-growth250-tracker が yfinance で生成しロリポップに配信。確定値)
    すなわち「構成銘柄の騰落数(breadth)」と「主な上昇・下落銘柄」のみ。
  - 指数の終値・前日比・上昇/下落の断定は **書かない**
    (正確な指数値を確定的に取得できないため。ウェブ検索は古い記事の数値を
     拾う事故があったため使わない。誤った数値を出さないことを最優先する)

処理の流れ:
  1. growth250-data.json を取得し、主役銘柄と騰落数を抽出
  2. それを材料として Claude に渡し(外部検索なし)、
     「ブログ記事(タイトル+本文)」と「ツイート(リンクなし)」を一括生成
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
import time
import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import anthropic
import tweepy

JST = ZoneInfo("Asia/Tokyo")

# moo-growth250-tracker がロリポップに配信している構成銘柄データ
JSON_URL = "https://moo-stock-blog.com/growth250-data.json"
# トラッカーに統合した場合、同じワークフローで生成されるローカルファイル
# (scripts/post_growth250.py から見た repo-root/public/growth250-data.json)
LOCAL_JSON = Path(__file__).resolve().parent.parent / "public" / "growth250-data.json"

MODEL = "claude-haiku-4-5-20251001"           # 本文生成(軽量モデル)
TWEET_LIMIT = 140                             # 全角換算のおおよその上限(無印アカウント)
# ツイート末尾の固定フッター(URLは載せない方針)
TWEET_FOOTER = "詳細はプロフィールのリンクから確認してください"
# フッター+空行ぶんを引いた、本文に使える全角字数の予算
TWEET_BODY_LIMIT = TWEET_LIMIT - len(TWEET_FOOTER) - 2  # ≒114字


# --------------------------------------------------------------------------
# 1. 自前JSONから主役銘柄・騰落数を取得
# --------------------------------------------------------------------------
def fetch_market_data() -> dict | None:
    """growth250-data.json を読み、主役銘柄とbreadthを返す。失敗時 None。

    1) 同じワークフローで生成されたローカルファイルを優先(接続不要)
    2) 無ければロリポップ上のURLからHTTP取得
    """
    data = None

    # 1) ローカルファイル(統合運用時)
    try:
        if LOCAL_JSON.exists():
            data = json.loads(LOCAL_JSON.read_text(encoding="utf-8"))
            print(f"[info] ローカルJSONを読み込み: {LOCAL_JSON}")
    except Exception as e:
        print(f"[warn] ローカルJSON読み込み失敗: {e}", file=sys.stderr)

    # 2) HTTPフォールバック
    if data is None:
        try:
            r = requests.get(JSON_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            print("[info] HTTPでJSON取得")
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
    """Xの重み付き文字数(ざっくり): ASCIIは1、それ以外(全角等)は2。
    URLはX側でt.coに短縮され23固定になるので、計測でも23扱いにする。"""
    # URLを23文字分のプレースホルダに置換してから数える
    text_for_count = re.sub(r"https?://\S+", "X" * 23, text)
    return sum(1 if ord(ch) < 0x1100 else 2 for ch in text_for_count)


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

    prompt = f"""あなたは日本株の市況コメントを書く編集者です。

本日({today_label})の東証グロース市場250指数(グロース250)について、構成銘柄の概況を書きます。
以下は当日の構成銘柄データ(自前集計・正確)です。**この値だけを根拠に書いてください**:
  値上がり {m['up']}銘柄 / 値下がり {m['down']}銘柄 / 変わらず {m['flat']}銘柄(全{m['total']}銘柄)
  上昇上位: {_fmt_movers(m['best'])}
  下落上位: {_fmt_movers(m['worst'])}

**厳守事項:**
  - **指数の終値・前日比・騰落率などの“指数の数値”は一切書かない**(手元に正確な値が無いため)。
  - **指数が上がった/下がった(反発・続落等)とも断定しない**(指数は時価総額加重で、騰落数とは
    一致しないため、上のデータからは判定できない)。
  - 述べてよいのは「**構成銘柄の騰落数(breadth)**」と「**個別の主役銘柄**」のみ。
    例:「構成銘柄は値下がり{m['down']}・値上がり{m['up']}とやや値下がり優勢」。
  - 騰落数は正しく表現する({m['down']}>{m['up']}なら“値下がり優勢”、逆なら“値上がり優勢”)。
  - **割合(◯%)は自分で計算・記載しない**(計算ミスのもと)。使うのは生の銘柄数だけ
    (値上がり{m['up']}・値下がり{m['down']}・変わらず{m['flat']})。割合を言いたい時は
    「半数弱」「過半」など概数の言葉でとどめる。
  - 上昇/下落銘柄の顔ぶれから読み取れる**テーマ(例:宇宙関連、AI関連 等)**には触れてよいが、
    それは上の銘柄名から判断できる範囲に限る。外部の出来事(米国市場や個別ニュース)は
    **データに無いので書かない**(推測・創作の禁止)。
  - 数値・銘柄名・騰落率は上のデータの通りに正確に。

次の3つを**JSONだけ**で出力(コードフェンス・前置き・説明は一切不要):
{{
  "title": "記事見出し。日付と、騰落数の傾向や主役銘柄を含め30〜45字程度。指数の数値や上昇/下落の断定は入れない。",
  "body_html": "<p>段落</p> を2個程度。1段落目で構成銘柄の騰落傾向、2段落目で主な上昇・下落銘柄(と読み取れるテーマ)。事実ベースで簡潔に。タグは<p>と<strong>程度のみ。",
  "tweet": "ツイート本文。全角{TWEET_BODY_LIMIT}字以内、ただし**{TWEET_BODY_LIMIT - 15}字以上を目安にしっかり書く**(短すぎ注意)。騰落数に加えて、上昇側・下落側それぞれ2〜3銘柄の名前と騰落率、読み取れるテーマまで盛り込む。**リンクは入れない**。末尾に #グロース250 #新興市場 を付けてよい。指数の数値・方向断定は入れない。"
}}
JSON以外は出力しないこと。"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    obj = _parse_json(_extract_text(resp))

    # ツイートが長すぎる場合は1回だけ短縮
    if weighted_len(obj.get("tweet", "")) > TWEET_BODY_LIMIT * 2:
        resp2 = client.messages.create(
            model=MODEL,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": f"次のツイートを、意味を保ったまま全角{TWEET_BODY_LIMIT}字以内に。"
                           f"リンクは入れない。本文のみ出力:\n\n{obj['tweet']}",
            }],
        )
        obj["tweet"] = _extract_text(resp2)
    return obj


# --------------------------------------------------------------------------
# 3a. WordPress に本物の記事として投稿（タイムアウト時はリトライ）
# --------------------------------------------------------------------------
def post_to_wordpress(title: str, body_html: str) -> str:
    base = os.environ["WP_BASE_URL"].rstrip("/")
    url = f"{base}/wp-json/wp/v2/posts"
    payload = {"title": title, "content": body_html, "status": "publish"}
    cat = os.environ.get("WP_CATEGORY_ID")
    if cat:
        payload["categories"] = [int(cat)]

    # ロリポップは時々重い。タイムアウトしたら最大3回までリトライ。
    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            r = requests.post(
                url,
                json=payload,
                auth=(os.environ["WP_USER"], os.environ["WP_APP_PASSWORD"]),
                timeout=(30, 60),  # (connect, read)
            )
            r.raise_for_status()
            return r.json().get("link", "(URL不明)")
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            wait = 15 * attempt  # 15秒 → 30秒 → 45秒
            print(f"[warn] WP投稿 試行{attempt}/3 失敗({type(e).__name__})。{wait}秒待って再試行", file=sys.stderr)
            if attempt < 3:
                time.sleep(wait)
        except Exception as e:
            # タイムアウト系以外(認証エラー等)はリトライしても無意味なので即諦める
            raise
    # 3回とも失敗
    raise last_err  # type: ignore


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
        wp_link = post_to_wordpress(title, body)
        print(f"[wordpress] 公開しました: {wp_link}")
    except Exception as e:
        ok = False
        print(f"[error] WordPress投稿に失敗: {e}", file=sys.stderr)

    # ツイート末尾に固定フッターを付ける(URLは載せない方針)
    final_tweet = tweet
    candidate = f"{tweet}\n\n{TWEET_FOOTER}"
    if weighted_len(candidate) <= TWEET_LIMIT * 2:
        final_tweet = candidate
    else:
        print(f"[warn] フッター付与で重み{weighted_len(candidate)}>{TWEET_LIMIT*2}のためフッターなしで投稿", file=sys.stderr)
    print(f"[info] 最終ツイート(重み付き{weighted_len(final_tweet)}/{TWEET_LIMIT*2})")

    try:
        resp = post_to_x(final_tweet)
        tid = resp.data.get("id") if resp and resp.data else "?"
        print(f"[x] 投稿しました: tweet id = {tid}")
    except Exception as e:
        ok = False
        print(f"[error] X投稿に失敗: {e}", file=sys.stderr)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
