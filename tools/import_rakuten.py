"""楽天証券の保有商品 CSV を HOLDINGS_JSON 形式に変換するユーティリティ。

Usage:
    python tools/import_rakuten.py <csv-file> [<csv-file>...]

サポート対象 (どちらか / 両方を一度に渡せる):
    - 国内株式の保有商品一覧 CSV
    - 投資信託の保有商品一覧 CSV

楽天証券Web > マイメニュー > 保有商品一覧 > CSV ダウンロード で取得
（CP932/Shift-JIS 想定。UTF-8 でも可）。

出力:
    HOLDINGS_JSON 形式の JSON を標準出力に出力。これを GitHub Secret
    `HOLDINGS_JSON` に貼り付ける。

集約ルール:
    - 同一銘柄が複数行（一般 / 特定 / NISA など）に分かれている場合、
      数量を合計し平均取得単価は数量加重平均で算出。
    - 投信は同一ファンド名を重複排除し name だけ出力。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


SYMBOL_COL = ["銘柄コード", "コード"]
NAME_COL_STOCK = ["銘柄名", "銘柄"]
SHARES_COL = ["保有数量", "数量", "残数量", "残高"]
COST_COL = ["平均取得単価", "取得単価", "取得平均単価", "平均単価"]
NAME_COL_FUND = ["ファンド名", "投資信託", "銘柄名"]


def _decode(raw: bytes) -> str:
    for encoding in ("cp932", "shift_jis", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError("Cannot decode CSV (tried cp932/shift_jis/utf-8)")


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """CSV を読み込み、ヘッダー行と本体行を返す。

    楽天 CSV は冒頭にメタデータ行が入る場合があるので、キー列名を含む
    最初の行をヘッダーとして検出する。
    """
    text = _decode(path.read_bytes())
    rows = [r for r in csv.reader(text.splitlines()) if r]
    if not rows:
        return [], []
    keys = set(SYMBOL_COL + NAME_COL_STOCK + NAME_COL_FUND)
    header_idx: int | None = None
    for i, row in enumerate(rows):
        if any(cell.strip() in keys for cell in row):
            header_idx = i
            break
    if header_idx is None:
        return [], []
    header = [c.strip() for c in rows[header_idx]]
    return header, rows[header_idx + 1 :]


def _find_col(header: list[str], candidates: list[str]) -> int | None:
    for cand in candidates:
        if cand in header:
            return header.index(cand)
    for cand in candidates:
        for i, h in enumerate(header):
            if cand in h:
                return i
    return None


def _to_number(s: str) -> float | None:
    if s is None:
        return None
    cleaned = s.strip().replace(",", "").replace("円", "").replace("株", "")
    if not cleaned or cleaned in {"-", "--", "ー"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def detect_kind(header: list[str]) -> str | None:
    has_symbol = _find_col(header, SYMBOL_COL) is not None
    has_cost = _find_col(header, COST_COL) is not None
    if has_symbol and has_cost:
        return "stocks"
    if any("ファンド" in h or "投資信託" in h for h in header):
        return "funds"
    return None


def parse_stock_lots(
    header: list[str], rows: list[list[str]]
) -> list[tuple[str, str, float, float | None]]:
    """株式 CSV の各行を (symbol, name, shares, avg_cost_or_none) で返す。"""
    sym_i = _find_col(header, SYMBOL_COL)
    name_i = _find_col(header, NAME_COL_STOCK)
    shares_i = _find_col(header, SHARES_COL)
    cost_i = _find_col(header, COST_COL)
    if sym_i is None or shares_i is None:
        return []

    out: list[tuple[str, str, float, float | None]] = []
    for row in rows:
        max_idx = max(i for i in (sym_i, name_i, shares_i, cost_i) if i is not None)
        if len(row) <= max_idx:
            continue
        raw_sym = row[sym_i].strip()
        if not raw_sym or not raw_sym.isdigit():
            continue
        symbol = f"{raw_sym}.T"
        name = row[name_i].strip() if name_i is not None else symbol
        shares = _to_number(row[shares_i])
        if shares is None or shares <= 0:
            continue
        cost = _to_number(row[cost_i]) if cost_i is not None else None
        out.append((symbol, name, shares, cost))
    return out


def parse_fund_names(header: list[str], rows: list[list[str]]) -> list[str]:
    name_i = _find_col(header, NAME_COL_FUND)
    if name_i is None:
        return []
    out: list[str] = []
    for row in rows:
        if len(row) <= name_i:
            continue
        name = row[name_i].strip()
        if name and name not in out:
            out.append(name)
    return out


def aggregate_stocks(
    lots: list[tuple[str, str, float, float | None]],
) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "shares": 0.0, "cost_sum": 0.0, "cost_shares": 0.0}
    )
    for symbol, name, shares, cost in lots:
        a = agg[symbol]
        if not a["name"]:
            a["name"] = name
        a["shares"] += shares
        if cost is not None and cost > 0:
            a["cost_sum"] += shares * cost
            a["cost_shares"] += shares

    out: list[dict[str, Any]] = []
    for symbol, a in agg.items():
        item: dict[str, Any] = {"symbol": symbol, "name": a["name"]}
        item["shares"] = (
            int(a["shares"]) if float(a["shares"]).is_integer() else round(a["shares"], 4)
        )
        if a["cost_shares"] > 0:
            item["avg_cost"] = round(a["cost_sum"] / a["cost_shares"], 2)
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="楽天証券の保有商品 CSV を HOLDINGS_JSON 形式に変換",
    )
    parser.add_argument(
        "csv_files",
        nargs="+",
        help="楽天証券からダウンロードした CSV (株式 / 投信、複数可)",
    )
    args = parser.parse_args()

    all_lots: list[tuple[str, str, float, float | None]] = []
    fund_names: list[str] = []

    for raw in args.csv_files:
        path = Path(raw)
        if not path.exists():
            print(f"WARN: {path} not found", file=sys.stderr)
            continue
        header, data = _read_csv(path)
        if not header:
            print(f"WARN: {path} has no recognizable header", file=sys.stderr)
            continue
        kind = detect_kind(header)
        if kind == "stocks":
            lots = parse_stock_lots(header, data)
            all_lots.extend(lots)
            print(
                f"INFO: {path.name} → stocks {len(lots)} lots", file=sys.stderr
            )
        elif kind == "funds":
            names = parse_fund_names(header, data)
            for n in names:
                if n not in fund_names:
                    fund_names.append(n)
            print(
                f"INFO: {path.name} → funds {len(names)} names", file=sys.stderr
            )
        else:
            print(
                f"WARN: {path.name} kind unknown (header sample: {header[:6]})",
                file=sys.stderr,
            )

    holdings = {
        "stocks": aggregate_stocks(all_lots),
        "funds": [{"name": n} for n in fund_names],
    }
    print(json.dumps(holdings, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
