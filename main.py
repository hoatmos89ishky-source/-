"""Automated Daily Stock Recommender.

JST 平日朝に GitHub Actions から実行され、為替・米国市場・日本株テクニカル
指標を取得し、Claude が生成した相場見通しを Gmail (SMTP) で配信する。
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
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf
from anthropic import Anthropic


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

JST = timezone(timedelta(hours=9))

DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_MAIL_TO = "ho.atmos.89.ishky@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

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

# 日本市場・指数
JP_INDEX = ("^N225", "日経平均")

# 既定ウォッチリスト（要件記載の銘柄）
# value はセクター分類（"export" / "domestic" / "financial"）と銘柄名のタプル
WATCHLIST: dict[str, tuple[str, str]] = {
    "7203.T": ("export", "トヨタ自動車"),
    "7272.T": ("export", "ヤマハ発動機"),
    "4755.T": ("domestic", "楽天グループ"),
    "8306.T": ("financial", "三菱UFJフィナンシャル・グループ"),
}


# ---------------------------------------------------------------------------
# データ取得層
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
    """ドル円の直近1週間トレンドを取得。"""
    df = _download("JPY=X", period="10d", interval="1d").tail(5)
    closes = df["Close"].astype(float)
    first, last = float(closes.iloc[0]), float(closes.iloc[-1])
    change_pct = (last - first) / first * 100.0
    if change_pct >= 0.5:
        bias = "yen_weak"  # 円安方向
    elif change_pct <= -0.5:
        bias = "yen_strong"  # 円高方向
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
    """米国主要指数・銘柄の前日終値と前日比。"""
    out: list[dict[str, Any]] = []
    for symbol, label in US_TICKERS.items():
        df = _download(symbol, period="5d", interval="1d")
        closes = df["Close"].astype(float).dropna()
        if len(closes) < 2:
            continue
        prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
        change_pct = (last - prev) / prev * 100.0
        out.append(
            {
                "symbol": symbol,
                "name": label,
                "close": round(last, 2),
                "prev_close": round(prev, 2),
                "change_pct": round(change_pct, 2),
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
    """Wilder の RSI（直近値）を返す。"""
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
    sector: str
    close: float
    change_pct: float
    ma25: float | None
    deviation_pct_25ma: float | None
    ma26w: float | None
    rsi14: float | None
    is_pullback_candidate: bool
    date: str


def analyze_stock(symbol: str, sector: str, name: str) -> StockSnapshot | None:
    daily = _download(symbol, period="6mo", interval="1d")
    weekly = _download(symbol, period="2y", interval="1wk")
    d_closes = daily["Close"].astype(float).dropna()
    w_closes = weekly["Close"].astype(float).dropna()
    if d_closes.empty:
        return None

    last = float(d_closes.iloc[-1])
    prev = float(d_closes.iloc[-2]) if len(d_closes) >= 2 else last
    ma25 = calc_ma(d_closes, 25)
    deviation = (
        round((last - ma25) / ma25 * 100.0, 2) if ma25 is not None else None
    )
    ma26w = calc_ma(w_closes, 26)
    rsi14 = calc_rsi(d_closes, 14)

    is_pullback = (
        rsi14 is not None
        and deviation is not None
        and rsi14 <= 30.0
        and abs(deviation) <= 3.0
    )

    return StockSnapshot(
        symbol=symbol,
        name=name,
        sector=sector,
        close=round(last, 2),
        change_pct=round((last - prev) / prev * 100.0, 2),
        ma25=ma25,
        deviation_pct_25ma=deviation,
        ma26w=ma26w,
        rsi14=rsi14,
        is_pullback_candidate=is_pullback,
        date=d_closes.index[-1].strftime("%Y-%m-%d"),
    )


# ---------------------------------------------------------------------------
# シグナル判定（為替バイアス → セクター優先）
# ---------------------------------------------------------------------------

def sector_focus(fx_bias: str) -> list[str]:
    """為替バイアスから優先セクターを返す。"""
    if fx_bias == "yen_weak":
        return ["export"]
    if fx_bias == "yen_strong":
        return ["domestic", "financial"]
    return []


def rank_watchlist(
    snapshots: list[StockSnapshot], priority_sectors: list[str]
) -> list[StockSnapshot]:
    """優先セクターを先頭に、押し目買い候補を上位に並べ替える。"""
    def key(s: StockSnapshot) -> tuple[int, int, float]:
        sector_rank = (
            priority_sectors.index(s.sector)
            if s.sector in priority_sectors
            else len(priority_sectors)
        )
        pullback_rank = 0 if s.is_pullback_candidate else 1
        rsi_rank = s.rsi14 if s.rsi14 is not None else 100.0
        return (sector_rank, pullback_rank, rsi_rank)

    return sorted(snapshots, key=key)


# ---------------------------------------------------------------------------
# レポート生成（Claude）
# ---------------------------------------------------------------------------

def build_prompt(
    fx: dict[str, Any],
    us: list[dict[str, Any]],
    jp_index: dict[str, Any],
    snapshots: list[StockSnapshot],
    priority_sectors: list[str],
) -> str:
    payload = {
        "as_of_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "fx": fx,
        "us_market_prev_close": us,
        "jp_index": jp_index,
        "watchlist": [asdict(s) for s in snapshots],
        "priority_sectors": priority_sectors,
        "pullback_candidates": [
            s.symbol for s in snapshots if s.is_pullback_candidate
        ],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    return (
        "あなたは日本株を専門とする経験豊富な相場アナリストです。"
        "以下の JSON データ（為替・米国市場前日終値・日経平均・"
        "日本株ウォッチリストのテクニカル指標）を踏まえ、"
        "本日（東京市場寄り付き前）のレポートを日本語で作成してください。\n\n"
        "出力は次のセクション構成で、合計1200文字以内・LINE で読みやすい体裁に:\n"
        "【相場見通し】 3〜4行で本日の地合いを総括。\n"
        "【為替・外部環境】 ドル円バイアスと米国市場の影響を簡潔に。\n"
        "【注目銘柄】 ウォッチリストから1〜3銘柄を選び、"
        "各銘柄について『銘柄名(コード) / 終値 / 25MA乖離率 / RSI / 26週MA との位置関係』"
        "を必ず根拠として明示し、押し目買い候補があれば優先的に取り上げる。\n"
        "数値は提供データのみを使用し、推測しないこと。"
        "末尾の免責事項はこちらで付与するので本文には含めないこと。\n\n"
        f"```json\n{data}\n```"
    )


def generate_report(prompt: str, *, model: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    parts: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    body = "\n".join(parts).strip()
    return f"{body}\n\n{DISCLAIMER}"


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

def collect_snapshots() -> list[StockSnapshot]:
    out: list[StockSnapshot] = []
    for symbol, (sector, name) in WATCHLIST.items():
        snap = analyze_stock(symbol, sector, name)
        if snap is not None:
            out.append(snap)
    return out


def run(*, dry_run: bool, skip_llm: bool, skip_notify: bool, model: str) -> int:
    fx = fetch_fx()
    us = fetch_us_market()
    jp_index = fetch_jp_index()
    snapshots = collect_snapshots()
    priority = sector_focus(fx["bias"])
    ranked = rank_watchlist(snapshots, priority)

    print("=== FX ===")
    print(json.dumps(fx, ensure_ascii=False, indent=2))
    print("=== US Market (prev close) ===")
    print(json.dumps(us, ensure_ascii=False, indent=2))
    print("=== JP Index ===")
    print(json.dumps(jp_index, ensure_ascii=False, indent=2))
    print("=== Watchlist ===")
    print(json.dumps([asdict(s) for s in ranked], ensure_ascii=False, indent=2))
    print(f"=== Priority sectors: {priority} ===")

    if dry_run:
        return 0

    if skip_llm:
        report = "（LLM スキップ: テクニカル要約のみ）\n" + json.dumps(
            {"fx_bias": fx["bias"], "priority": priority}, ensure_ascii=False
        )
        report = f"{report}\n\n{DISCLAIMER}"
    else:
        prompt = build_prompt(fx, us, jp_index, ranked, priority)
        report = generate_report(prompt, model=model)

    print("=== Report ===")
    print(report)

    if not skip_notify:
        subject = f"[Daily Stock Report] {datetime.now(JST).strftime('%Y-%m-%d')}"
        send_gmail(subject, report)
        print("=== Gmail sent ===")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily stock recommender")
    parser.add_argument("--dry-run", action="store_true",
                        help="データ取得とテクニカル計算のみ実行")
    parser.add_argument("--skip-llm", action="store_true",
                        help="Claude 呼び出しをスキップ")
    parser.add_argument("--skip-notify", action="store_true",
                        help="Gmail 通知をスキップ")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
                        help="使用する Claude モデル ID")
    args = parser.parse_args()

    try:
        return run(
            dry_run=args.dry_run,
            skip_llm=args.skip_llm,
            skip_notify=args.skip_notify,
            model=args.model,
        )
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if not args.dry_run and not args.skip_notify:
            try:
                send_gmail(
                    "[Daily Stock Report] ERROR",
                    "Daily Stock Recommender でエラーが発生しました。\n"
                    "GitHub Actions のログを確認してください。\n\n"
                    f"{tb}",
                )
            except Exception:  # noqa: BLE001
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
