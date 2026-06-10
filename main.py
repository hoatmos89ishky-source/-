"""Automated Daily Stock Recommender (v2).

JST 平日 07:30 (UTC 22:30) に GitHub Actions cron で実行され、為替・米国市場・
日経主要構成銘柄のテクニカル指標と、ユーザーの保有株・保有投信の動向を
取得し、Claude (web_search ツール経由で最新決算・ニュース・世情を踏まえる)
が生成したレポートを Gmail で配信する。
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
import traceback
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone, timedelta
from email.message import EmailMessage
from typing import Any

import jpholiday
import numpy as np
import pandas as pd
import yfinance as yf
from anthropic import Anthropic


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

JST = timezone(timedelta(hours=9))

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAIL_TO = "ho.atmos.89.ishky@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# 東証は12/31〜1/3が休場（祝日でなくても）
YEAR_END_NEW_YEAR: set[tuple[int, int]] = {(12, 31), (1, 1), (1, 2), (1, 3)}

DISCLAIMER = (
    "※本情報は投資勧誘を目的としたものではなく、"
    "投資の最終決定はご自身の判断で行ってください。"
)

# 米国市場・指数（前日終値）
US_TICKERS: dict[str, str] = {
    "^DJI": "NYダウ",
    "^IXIC": "NASDAQ総合",
    "^SOX": "SOX指数",
    "NVDA": "NVIDIA",
}

JP_INDEX = ("^N225", "日経平均")

# 日経225 構成銘柄から流動性・知名度上位 30 銘柄
JP_UNIVERSE: list[tuple[str, str]] = [
    ("7203.T", "トヨタ自動車"),
    ("6758.T", "ソニーグループ"),
    ("9984.T", "ソフトバンクグループ"),
    ("9983.T", "ファーストリテイリング"),
    ("8035.T", "東京エレクトロン"),
    ("4063.T", "信越化学工業"),
    ("8306.T", "三菱UFJフィナンシャル・グループ"),
    ("9433.T", "KDDI"),
    ("9432.T", "NTT"),
    ("6861.T", "キーエンス"),
    ("7974.T", "任天堂"),
    ("6098.T", "リクルートホールディングス"),
    ("6594.T", "ニデック"),
    ("4502.T", "武田薬品工業"),
    ("4568.T", "第一三共"),
    ("8316.T", "三井住友フィナンシャルグループ"),
    ("8411.T", "みずほフィナンシャルグループ"),
    ("8001.T", "伊藤忠商事"),
    ("8058.T", "三菱商事"),
    ("8031.T", "三井物産"),
    ("6501.T", "日立製作所"),
    ("6902.T", "デンソー"),
    ("7267.T", "本田技研工業"),
    ("6954.T", "ファナック"),
    ("6981.T", "村田製作所"),
    ("6367.T", "ダイキン工業"),
    ("4452.T", "花王"),
    ("2914.T", "JT"),
    ("9020.T", "JR東日本"),
    ("9022.T", "JR東海"),
]


# ---------------------------------------------------------------------------
# データ取得層（yfinance ラッパー）
# ---------------------------------------------------------------------------

def _download(ticker: str, *, period: str, interval: str) -> pd.DataFrame:
    """yfinance ラッパー（指数バックオフで最大3回リトライ）。"""
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            df = yf.download(
                ticker,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=False,
            )
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(2 ** attempt)
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"empty data for {ticker}")


def fetch_fx() -> dict[str, Any]:
    df = _download("JPY=X", period="10d", interval="1d").tail(5)
    closes = df["Close"].astype(float)
    first, last = float(closes.iloc[0]), float(closes.iloc[-1])
    change_pct = (last - first) / first * 100.0
    if change_pct >= 0.5:
        bias = "yen_weak"
    elif change_pct <= -0.5:
        bias = "yen_strong"
    else:
        bias = "neutral"
    return {
        "symbol": "USD/JPY",
        "series": [
            {"date": idx.strftime("%Y-%m-%d"), "close": round(float(v), 3)}
            for idx, v in closes.items()
        ],
        "week_change_pct": round(change_pct, 2),
        "bias": bias,
    }


def fetch_us_market() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for symbol, label in US_TICKERS.items():
        try:
            df = _download(symbol, period="5d", interval="1d")
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: us {symbol} failed: {exc}", file=sys.stderr)
            continue
        closes = df["Close"].astype(float).dropna()
        if len(closes) < 2:
            continue
        prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
        out.append(
            {
                "symbol": symbol,
                "name": label,
                "close": round(last, 2),
                "prev_close": round(prev, 2),
                "change_pct": round((last - prev) / prev * 100.0, 2),
                "date": closes.index[-1].strftime("%Y-%m-%d"),
            }
        )
    return out


def fetch_jp_index() -> dict[str, Any]:
    symbol, label = JP_INDEX
    df = _download(symbol, period="3mo", interval="1d")
    closes = df["Close"].astype(float).dropna()
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
    ma25 = float(closes.rolling(25).mean().iloc[-1])
    return {
        "symbol": symbol,
        "name": label,
        "close": round(last, 2),
        "prev_close": round(prev, 2),
        "change_pct": round((last - prev) / prev * 100.0, 2),
        "ma25": round(ma25, 2),
        "deviation_pct": round((last - ma25) / ma25 * 100.0, 2),
        "date": closes.index[-1].strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# テクニカル指標
# ---------------------------------------------------------------------------

def calc_rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    val = rsi.iloc[-1]
    return None if pd.isna(val) else round(float(val), 2)


def calc_ma(closes: pd.Series, window: int) -> float | None:
    if len(closes) < window:
        return None
    val = closes.rolling(window).mean().iloc[-1]
    return None if pd.isna(val) else round(float(val), 2)


@dataclass
class StockSnapshot:
    symbol: str
    name: str
    close: float
    change_pct_1d: float
    change_pct_5d: float | None
    change_pct_30d: float | None
    ma25: float | None
    deviation_pct_25ma: float | None
    ma26w: float | None
    rsi14: float | None
    date: str


def analyze_stock(symbol: str, name: str) -> StockSnapshot | None:
    try:
        daily = _download(symbol, period="6mo", interval="1d")
        weekly = _download(symbol, period="2y", interval="1wk")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: analyze_stock failed for {symbol}: {exc}", file=sys.stderr)
        return None

    d_closes = daily["Close"].astype(float).dropna()
    w_closes = weekly["Close"].astype(float).dropna()
    if len(d_closes) < 2:
        return None

    last = float(d_closes.iloc[-1])
    prev = float(d_closes.iloc[-2])
    ma25 = calc_ma(d_closes, 25)
    deviation = (
        round((last - ma25) / ma25 * 100.0, 2) if ma25 is not None else None
    )

    def pct_back(n: int) -> float | None:
        if len(d_closes) > n:
            base = float(d_closes.iloc[-1 - n])
            if base > 0:
                return round((last - base) / base * 100.0, 2)
        return None

    return StockSnapshot(
        symbol=symbol,
        name=name,
        close=round(last, 2),
        change_pct_1d=round((last - prev) / prev * 100.0, 2),
        change_pct_5d=pct_back(5),
        change_pct_30d=pct_back(20),  # 20 営業日 ≒ 1ヶ月
        ma25=ma25,
        deviation_pct_25ma=deviation,
        ma26w=calc_ma(w_closes, 26),
        rsi14=calc_rsi(d_closes, 14),
        date=d_closes.index[-1].strftime("%Y-%m-%d"),
    )


def screen_universe() -> list[StockSnapshot]:
    out: list[StockSnapshot] = []
    for symbol, name in JP_UNIVERSE:
        snap = analyze_stock(symbol, name)
        if snap is not None:
            out.append(snap)
    return out


# ---------------------------------------------------------------------------
# 保有情報
# ---------------------------------------------------------------------------

def load_holdings() -> dict[str, list[dict[str, Any]]]:
    raw = os.environ.get("HOLDINGS_JSON", "")
    if not raw.strip():
        return {"stocks": [], "funds": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"WARN: HOLDINGS_JSON parse error: {exc}", file=sys.stderr)
        return {"stocks": [], "funds": []}
    return {
        "stocks": list(data.get("stocks", []) or []),
        "funds": list(data.get("funds", []) or []),
    }


def _fetch_news_titles(symbol: str) -> list[str]:
    try:
        ticker = yf.Ticker(symbol)
        news_list = getattr(ticker, "news", None) or []
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: news fetch failed for {symbol}: {exc}", file=sys.stderr)
        return []
    titles: list[str] = []
    for n in news_list[:5]:
        if not isinstance(n, dict):
            continue
        title = n.get("title")
        if not title and isinstance(n.get("content"), dict):
            title = n["content"].get("title")
        if title:
            titles.append(str(title))
    return titles


def _fetch_next_earnings(symbol: str) -> str | None:
    try:
        ticker = yf.Ticker(symbol)
        cal = getattr(ticker, "calendar", None)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: calendar fetch failed for {symbol}: {exc}", file=sys.stderr)
        return None
    if cal is None:
        return None
    try:
        if isinstance(cal, pd.DataFrame) and not cal.empty:
            if "Earnings Date" in cal.index:
                v = cal.loc["Earnings Date"].iloc[0]
                return str(v)[:10]
            return None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, list) and ed:
                return str(ed[0])[:10]
            if ed:
                return str(ed)[:10]
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: calendar parse failed for {symbol}: {exc}", file=sys.stderr)
    return None


def fetch_holding_stock_context(stock: dict[str, Any]) -> dict[str, Any]:
    symbol = stock.get("symbol", "")
    name = stock.get("name") or symbol
    shares = stock.get("shares")
    avg_cost = stock.get("avg_cost")

    snap = analyze_stock(symbol, name)
    if snap is None:
        return {"symbol": symbol, "name": name, "error": "data unavailable"}

    ctx = asdict(snap)
    ctx["shares"] = shares
    ctx["avg_cost"] = avg_cost
    if (
        shares is not None
        and avg_cost is not None
        and isinstance(avg_cost, (int, float))
        and avg_cost > 0
    ):
        unrealized = (snap.close - avg_cost) * shares
        ctx["unrealized_pl"] = round(float(unrealized), 2)
        ctx["unrealized_pl_pct"] = round(
            (snap.close - avg_cost) / avg_cost * 100.0, 2
        )

    ctx["news_titles"] = _fetch_news_titles(symbol)
    ctx["next_earnings"] = _fetch_next_earnings(symbol)
    return ctx


# ---------------------------------------------------------------------------
# レポート生成（Claude + Web Search Tool）
# ---------------------------------------------------------------------------

def build_prompt(
    fx: dict[str, Any],
    us: list[dict[str, Any]],
    jp_index: dict[str, Any],
    universe: list[StockSnapshot],
    holdings_ctx: dict[str, Any],
) -> str:
    payload = {
        "as_of_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "fx": fx,
        "us_market_prev_close": us,
        "jp_index": jp_index,
        "universe_snapshots": [asdict(s) for s in universe],
        "holdings": holdings_ctx,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2)

    has_stocks = bool(holdings_ctx.get("stocks"))
    has_funds = bool(holdings_ctx.get("funds"))
    if not has_stocks and not has_funds:
        holdings_note = "保有情報は未設定のため、セクション4・5・6は省略してください。\n"
    elif not has_funds:
        holdings_note = "保有投信が未設定のため、セクション6は省略してください。\n"
    elif not has_stocks:
        holdings_note = "保有株が未設定のため、セクション4・5は省略してください。\n"
    else:
        holdings_note = ""

    return (
        "あなたは経験豊富な日本株アナリスト兼ポートフォリオマネージャーです。"
        "本日（JST 朝、東京市場寄り付き前）に Gmail で読みやすい日本語レポートを作成してください。\n\n"
        "必要に応じて web_search ツールを使い、保有銘柄の最新決算 / IR / ニュース / 世の情勢を確認し、"
        "売却タイミング判断は鋭く具体的に。投資信託はファンド名から最新の基準価額方向感と関連ニュースを"
        "Web 検索でコメントすること。\n\n"
        "出力フォーマット（合計 2000 文字以内、見出しは【】で囲む）:\n"
        "1. 【相場見通し】 3〜4行で本日の地合いを総括。\n"
        "2. 【為替・米国市場】 ドル円バイアスと米国前日終値の影響を簡潔に。\n"
        "3. 【今日のおすすめ10銘柄】 universe_snapshots から銘柄を10件選び、"
        "『銘柄名(コード) / 終値 / 1〜2行の理由』を箇条書きで。"
        "為替バイアス（円安なら輸出、円高なら内需・金融）と RSI / 25MA 乖離率を踏まえて選定。\n"
        "4. 【保有銘柄の動向】 各保有株について"
        "『銘柄名(コード) / 終値 / 1日 / 5日 / 30日 / 含み損益(円・％) / RSI / 25MA乖離率』を1行で。\n"
        "5. 【保有銘柄の売却判断】 各保有株を HOLD / WATCH / TRIM / SELL の4段階で判定し、"
        "決算予定・最新ニュース・テクニカル・マクロ環境を踏まえた根拠を1〜2行で。\n"
        "6. 【保有投信のコメント】 ファンド名ごとに Web 検索結果ベースで「直近の方向感・注目材料」を1〜2行で。\n\n"
        f"{holdings_note}"
        "注意: 数値は提供 JSON を優先し、推測時は『推定』と明記。"
        "本日が日本の祝日（東証休場）と思われる場合は冒頭にその旨を注記。"
        "末尾の免責事項はプログラム側で付与するので本文には含めない。\n\n"
        f"```json\n{data}\n```"
    )


def generate_report(prompt: str, *, model: str, use_web_search: bool = True) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = Anthropic(api_key=api_key)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_web_search:
        kwargs["tools"] = [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8,
            }
        ]

    resp = client.messages.create(**kwargs)

    # web_search は server-side ツールのため通常は1回の呼び出しで完結する。
    # 念のため stop_reason が tool_use のままなら、assistant の content を
    # 会話に追加して継続呼び出しする（safety loop）。
    safety = 0
    while getattr(resp, "stop_reason", None) == "tool_use" and safety < 5:
        safety += 1
        kwargs["messages"] = kwargs["messages"] + [
            {"role": "assistant", "content": resp.content}
        ]
        resp = client.messages.create(**kwargs)

    parts: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    body = "\n".join(parts).strip()
    return f"{body}\n\n{DISCLAIMER}"


# ---------------------------------------------------------------------------
# 東証休場日判定
# ---------------------------------------------------------------------------

def is_tse_closed(d: date) -> bool:
    """土日 / 日本の祝日 / 年末年始(12/31〜1/3) で東証が休場かを返す。"""
    if d.weekday() >= 5:  # 土(5) / 日(6)
        return True
    if (d.month, d.day) in YEAR_END_NEW_YEAR:
        return True
    if jpholiday.is_holiday(d):
        return True
    return False


# ---------------------------------------------------------------------------
# エラー分類（初心者向けの説明文を返す）
# ---------------------------------------------------------------------------

ERROR_FOOTER = (
    "\n対応後、GitHub の Actions タブ →「Daily Stock Report」→\n"
    "「Run workflow」から手動で再実行できます。"
)


def classify_error(exc: Exception) -> tuple[str, str]:
    """例外を見て、人に伝わる (件名, 本文) を返す。"""
    msg = str(exc).lower()

    if "credit balance is too low" in msg or "insufficient_quota" in msg:
        return (
            "[Daily Stock Report] エラー: Anthropic API クレジット残高不足",
            "本日のレポート生成を中止しました。\n\n"
            "■ 原因\n"
            "Anthropic API のクレジット残高が不足しています。\n\n"
            "■ 対処\n"
            "1. https://console.anthropic.com にログイン\n"
            "2. 左メニューの「Plans & Billing」をクリック\n"
            "3. 「Add credits」からクレジット ($5 以上) を追加\n"
            "4. 必要に応じて「Auto-recharge」を有効化"
            + ERROR_FOOTER,
        )

    if (
        "invalid x-api-key" in msg
        or "invalid api key" in msg
        or "authentication_error" in msg
    ):
        return (
            "[Daily Stock Report] エラー: Anthropic API キーが無効",
            "本日のレポート生成を中止しました。\n\n"
            "■ 原因\n"
            "Anthropic API キー (ANTHROPIC_API_KEY) が無効か失効しています。\n\n"
            "■ 対処\n"
            "1. https://console.anthropic.com → API Keys から新規キーを発行\n"
            "2. GitHub > Settings > Secrets and variables > Actions で\n"
            "   ANTHROPIC_API_KEY を更新"
            + ERROR_FOOTER,
        )

    if "rate_limit" in msg or "rate limit" in msg or " 429" in msg:
        return (
            "[Daily Stock Report] エラー: Anthropic API レート制限",
            "本日のレポート生成を中止しました。\n\n"
            "■ 原因\n"
            "Anthropic API のレート制限に達しました。\n\n"
            "■ 対処\n"
            "数十分〜数時間後に再実行してください。\n"
            "頻発する場合は Plans & Billing から Tier をアップグレードしてください。"
            + ERROR_FOOTER,
        )

    if (
        "smtpauthentication" in msg
        or "username and password not accepted" in msg
        or "smtpexception" in msg
    ):
        return (
            "[Daily Stock Report] エラー: Gmail 送信失敗",
            "Gmail への送信処理でエラーが発生しました。\n\n"
            "■ 原因\n"
            "Gmail アプリパスワード (GMAIL_APP_PASSWORD) が無効、\n"
            "または送信元アドレス (GMAIL_SENDER) と不一致の可能性があります。\n\n"
            "■ 対処\n"
            "1. https://myaccount.google.com/apppasswords でパスワードを再発行\n"
            "2. GitHub Secret の GMAIL_APP_PASSWORD を更新（16文字、スペース無し）"
            + ERROR_FOOTER,
        )

    tb = traceback.format_exc()
    return (
        "[Daily Stock Report] ERROR",
        "Daily Stock Recommender でエラーが発生しました。\n"
        "GitHub Actions のログを確認してください。\n\n"
        f"{tb}",
    )


# ---------------------------------------------------------------------------
# Gmail 通知（SMTP + アプリパスワード）
# ---------------------------------------------------------------------------

def send_gmail(subject: str, body: str) -> None:
    sender = os.environ.get("GMAIL_SENDER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("GMAIL_TO", DEFAULT_MAIL_TO)
    if not sender or not password:
        raise RuntimeError(
            "GMAIL_SENDER and GMAIL_APP_PASSWORD must be set"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(sender, password)
        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def run(
    *,
    dry_run: bool,
    skip_llm: bool,
    skip_notify: bool,
    model: str,
    no_web_search: bool,
    force: bool,
) -> int:
    started = datetime.now(JST)
    print(f"=== Run start: {started.strftime('%Y-%m-%d %H:%M:%S JST')} ===")

    today_jst = started.date()
    if is_tse_closed(today_jst) and not force:
        print(
            f"=== TSE closed today ({today_jst}, "
            f"weekday={today_jst.strftime('%A')}). Skipping. ==="
        )
        return 0

    fx = fetch_fx()
    us = fetch_us_market()
    jp_index = fetch_jp_index()
    print("=== FX ===")
    print(json.dumps(fx, ensure_ascii=False, indent=2))
    print("=== US Market (prev close) ===")
    print(json.dumps(us, ensure_ascii=False, indent=2))
    print("=== JP Index ===")
    print(json.dumps(jp_index, ensure_ascii=False, indent=2))

    universe = screen_universe()
    print(f"=== Universe screened: {len(universe)}/{len(JP_UNIVERSE)} stocks ===")

    holdings_raw = load_holdings()
    print(
        f"=== Holdings input: {len(holdings_raw['stocks'])} stocks, "
        f"{len(holdings_raw['funds'])} funds ==="
    )
    holdings_ctx: dict[str, Any] = {
        "stocks": [fetch_holding_stock_context(s) for s in holdings_raw["stocks"]],
        "funds": holdings_raw["funds"],
    }

    if dry_run:
        # dry-run のときのみ保有情報の詳細を出力
        print("=== Universe snapshots (dry-run) ===")
        print(json.dumps([asdict(s) for s in universe], ensure_ascii=False, indent=2))
        print("=== Holdings context (dry-run) ===")
        print(json.dumps(holdings_ctx, ensure_ascii=False, indent=2))
        return 0

    if skip_llm:
        report = (
            "(LLM スキップ)\n"
            f"universe: {[s.symbol for s in universe]}\n"
            f"holdings stocks: {[s.get('symbol') for s in holdings_ctx['stocks']]}\n"
            f"holdings funds: {[f.get('name') for f in holdings_ctx['funds']]}\n\n"
            f"{DISCLAIMER}"
        )
    else:
        prompt = build_prompt(fx, us, jp_index, universe, holdings_ctx)
        report = generate_report(
            prompt, model=model, use_web_search=not no_web_search
        )

    print(f"=== Report length: {len(report)} chars ===")

    if not skip_notify:
        subject = f"[Daily Stock Report] {datetime.now(JST).strftime('%Y-%m-%d')}"
        send_gmail(subject, report)
        finished = datetime.now(JST)
        elapsed = (finished - started).total_seconds()
        print(
            f"=== Gmail sent at {finished.strftime('%H:%M:%S JST')} "
            f"(elapsed {elapsed:.1f}s) ==="
        )
    else:
        print("=== (skip notify) ===")
        preview = report[:500] + ("..." if len(report) > 500 else "")
        print(preview)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily stock recommender")
    parser.add_argument("--dry-run", action="store_true",
                        help="データ取得とテクニカル計算のみ実行")
    parser.add_argument("--skip-llm", action="store_true",
                        help="Claude 呼び出しをスキップ")
    parser.add_argument("--skip-notify", action="store_true",
                        help="Gmail 通知をスキップ")
    parser.add_argument("--no-web-search", action="store_true",
                        help="Claude の Web Search ツールを無効化")
    parser.add_argument("--force", action="store_true",
                        help="東証休場日でも強制実行する")
    parser.add_argument(
        "--model",
        default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
        help="使用する Claude モデル ID",
    )
    args = parser.parse_args()

    try:
        return run(
            dry_run=args.dry_run,
            skip_llm=args.skip_llm,
            skip_notify=args.skip_notify,
            model=args.model,
            no_web_search=args.no_web_search,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if not args.dry_run and not args.skip_notify:
            try:
                subject, body = classify_error(exc)
                send_gmail(subject, body)
            except Exception:  # noqa: BLE001
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
