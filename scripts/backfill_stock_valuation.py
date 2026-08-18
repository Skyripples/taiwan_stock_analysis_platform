"""Backfill five years of monthly official valuation observations."""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from config import PROJECT_ROOT


LOGGER = logging.getLogger("stock_valuation_backfill")
OUTPUT_DIR = PROJECT_ROOT / "data" / "stocks" / "history"
TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d"
TPEX_URL = "https://www.tpex.org.tw/web/stock/aftertrading/peratio_analysis/pera_result.php"
DEFAULT_SYMBOLS = ("2330", "2317", "6488")


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill monthly stock valuation")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--months", type=int, default=60)
    parser.add_argument("--delay", type=float, default=0.2)
    return parser.parse_args()


def atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def number(value: Any) -> int | float | None:
    text = str(value if value is not None else "").replace(",", "").strip()
    if text in {"", "--", "---", "N/A", "-"}: return None
    try:
        result = round(float(text), 6)
        return int(result) if result.is_integer() else result
    except ValueError: return None


def month_ends(months: int) -> list[date]:
    today = datetime.now(timezone.utc).date()
    cursor = date(today.year, today.month, 1)
    output: list[date] = []
    for offset in range(months - 1, -1, -1):
        year = cursor.year; month = cursor.month - offset
        while month <= 0: year -= 1; month += 12
        output.append(date(year, month, calendar.monthrange(year, month)[1]))
    return output


class Client:
    def __init__(self, delay: float) -> None:
        self.delay = delay; self.last = 0.0
        self.session = requests.Session(); self.session.headers["User-Agent"] = "taiwan-stock-analysis-platform/1.0"

    def get(self, url: str, params: dict[str, str]) -> Any:
        wait = self.delay - (time.monotonic() - self.last)
        if wait > 0: time.sleep(wait)
        try:
            response = self.session.get(url, params=params, timeout=30)
        except requests.exceptions.SSLError:
            response = self.session.get(url, params=params, timeout=30, verify=False)
        self.last = time.monotonic(); response.raise_for_status(); return response.json()


def fetch_month(client: Client, market: str, month_end: date) -> tuple[str, dict[str, list[Any]]] | None:
    for days_back in range(11):
        query = month_end - timedelta(days=days_back)
        try:
            if market == "TWSE":
                payload = client.get(TWSE_URL, {"date": query.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"})
                if payload.get("stat") != "OK" or payload.get("date") != query.strftime("%Y%m%d"): continue
                rows = payload.get("data", [])
            else:
                roc = f"{query.year - 1911:03d}/{query.month:02d}/{query.day:02d}"
                payload = client.get(TPEX_URL, {"d": roc, "l": "zh-tw", "o": "json", "s": "0"})
                table = payload.get("tables", [{}])[0]
                if table.get("date") != roc: continue
                rows = table.get("data", [])
            if not rows:
                continue
            return query.isoformat(), {str(row[0]).strip(): row for row in rows if isinstance(row, list) and len(row) >= 7}
        except Exception as exc:
            LOGGER.debug("%s %s unavailable: %s", market, query, exc)
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    options = args(); symbols = {str(item).strip() for item in options.symbols}; client = Client(options.delay)
    markets = {symbol: "TWSE" for symbol in symbols}
    # Market is read from existing caches when available; no symbol-specific hard coding.
    for symbol in symbols:
        path = PROJECT_ROOT / "data" / "stocks" / f"{symbol}.json"
        if path.exists():
            markets[symbol] = json.loads(path.read_text(encoding="utf-8"))["data"]["profile"]["market"]
    histories: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        existing_path = OUTPUT_DIR / f"{symbol}_valuation.json"
        if existing_path.exists():
            try:
                existing = json.loads(existing_path.read_text(encoding="utf-8"))["data"]["observations"]
                histories[symbol] = existing if isinstance(existing, list) else []
            except (KeyError, TypeError, json.JSONDecodeError):
                histories[symbol] = []
        else:
            histories[symbol] = []
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for position, month_end in enumerate(month_ends(options.months), 1):
        LOGGER.info("[%d/%d] %s", position, options.months, month_end.strftime("%Y-%m"))
        month_key = month_end.strftime("%Y-%m")
        snapshots = {}
        for market in set(markets.values()):
            market_symbols = [symbol for symbol in symbols if markets[symbol] == market]
            already_complete = all(any(row["trade_date"].startswith(month_key) for row in histories[symbol]) for symbol in market_symbols)
            snapshots[market] = None if already_complete else fetch_month(client, market, month_end)
        for symbol, market in markets.items():
            if any(row["trade_date"].startswith(month_key) for row in histories[symbol]): continue
            snapshot = snapshots.get(market)
            if not snapshot or symbol not in snapshot[1]: continue
            trade_date, row = snapshot[0], snapshot[1][symbol]
            if market == "TWSE":
                dividend_yield, pe, pb = number(row[3]), number(row[5]), number(row[6])
            else:
                pe, dividend_yield, pb = number(row[2]), number(row[5]), number(row[6])
            histories[symbol].append({"trade_date": trade_date, "pe": pe, "pb": pb, "dividend_yield": dividend_yield})
        # Partial checkpoints are valid observations and make rate-limited runs resumable.
        for symbol, rows in histories.items():
            if rows:
                atomic(OUTPUT_DIR / f"{symbol}_valuation.json", {"updated_at": updated_at, "provider": markets[symbol], "dataset": "stock_valuation_history", "version": "1.0", "frequency": "monthly_last_available_trading_day", "data": {"symbol": symbol, "observations": sorted(rows, key=lambda row: row["trade_date"])}})
    success = 0
    for symbol, rows in histories.items():
        path = OUTPUT_DIR / f"{symbol}_valuation.json"
        if len(rows) < 36:
            LOGGER.error("%s only has %d observations; existing history preserved", symbol, len(rows)); continue
        atomic(path, {"updated_at": updated_at, "provider": markets[symbol], "dataset": "stock_valuation_history", "version": "1.0", "frequency": "monthly_last_available_trading_day", "data": {"symbol": symbol, "observations": rows}})
        LOGGER.info("Updated %s | observations=%d | %s to %s", symbol, len(rows), rows[0]["trade_date"], rows[-1]["trade_date"]); success += 1
    return 0 if success else 1


if __name__ == "__main__": raise SystemExit(main())
