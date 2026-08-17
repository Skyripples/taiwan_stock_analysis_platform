"""Backfill historical market features without touching daily production files.

The script deliberately writes a row only when every required source is present.
Failures are recorded separately, so interrupted runs can safely resume and never
turn unavailable observations into zeroes.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import requests

from config import PROJECT_ROOT
from providers.taifex_night_futures_provider import (
    TaifexNightFuturesProvider,
    _NightMarketParser,
)
from providers.taifex_provider import TaifexProvider, _TableParser


LOGGER = logging.getLogger("historical_backfill")
TAIPEI_TZ = timezone(timedelta(hours=8))
HISTORY_DIR = PROJECT_ROOT / "data" / "history"
OUTPUT_PATH = HISTORY_DIR / "backfill_market_daily.csv"
FAILURE_PATH = HISTORY_DIR / "backfill_failures.csv"

TWSE_DAILY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_INSTITUTIONAL_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
TPEX_MONTHLY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingIndex"
TPEX_QUOTES_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
TAIFEX_POSITION_URL = "https://www.taifex.com.tw/cht/3/futContractsDate"
TAIFEX_NIGHT_URL = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

MARKET_FIELDS = (
    "trade_date",
    "prediction_target_date",
    "taiwan_market_trade_date",
    "institutional_trade_date",
    "foreign_futures_trade_date",
    "night_futures_trade_date",
    "tsm_adr_trade_date",
    "sox_trade_date",
    "sp500_trade_date",
    "nasdaq_trade_date",
    "nikkei_trade_date",
    "kospi_trade_date",
    "hang_seng_trade_date",
    "csi300_trade_date",
    "soxx_trade_date",
    "smh_trade_date",
    "nvda_trade_date",
    "amd_trade_date",
    "avgo_trade_date",
    "taiex_close",
    "taiex_change_percent",
    "tpex_close",
    "turnover",
    "advancing",
    "declining",
    "unchanged",
    "foreign_cash_flow",
    "foreign_futures_position",
    "night_futures_change",
    "tsm_adr_change_percent",
    "sox_change_percent",
    "sp500_change_percent",
    "nasdaq_change_percent",
    "nikkei_change_percent",
    "kospi_change_percent",
    "hang_seng_change_percent",
    "csi300_change_percent",
    "soxx_change_percent",
    "smh_change_percent",
    "nvda_change_percent",
    "amd_change_percent",
    "avgo_change_percent",
)
FAILURE_FIELDS = ("trade_date", "source", "error", "attempted_at")
YAHOO_SYMBOLS = {
    "tsm_adr": "TSM",
    "sox": "^SOX",
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "nikkei": "^N225",
    "kospi": "^KS11",
    "hang_seng": "^HSI",
    "csi300": "000300.SS",
    "soxx": "SOXX",
    "smh": "SMH",
    "nvda": "NVDA",
    "amd": "AMD",
    "avgo": "AVGO",
}


class SourceError(ValueError):
    """A recoverable source or validation failure."""


class RateLimitedSession:
    """HTTP session with a global delay and bounded exponential retries."""

    def __init__(self, delay: float, retries: int, timeout: float) -> None:
        self.delay = max(delay, 0.0)
        self.retries = max(retries, 0)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json,text/html,application/xhtml+xml",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
                "User-Agent": "taiwan-stock-analysis-platform-backfill/1.0",
            }
        )
        self._last_request = 0.0

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            wait = self.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
                self._last_request = time.monotonic()
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                return response
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                retry_after = 0.0
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    try:
                        retry_after = float(exc.response.headers.get("Retry-After", 0))
                    except ValueError:
                        retry_after = 0.0
                rate_limit_pause = 30.0 if (
                    isinstance(exc, requests.HTTPError)
                    and exc.response is not None
                    and exc.response.status_code == 429
                ) else 0.0
                time.sleep(max(retry_after, rate_limit_pause, min(2**attempt, 8)))
        raise SourceError(f"request failed after {self.retries + 1} attempts: {last_error}")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        try:
            return self.request("GET", url, **kwargs).json()
        except (requests.JSONDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"invalid JSON response: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill historical market features")
    parser.add_argument("--start", default="2020-01-01", help="YYYY-MM-DD")
    parser.add_argument("--end", default="today", help="YYYY-MM-DD or today")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds between requests")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--checkpoint", type=int, default=10)
    parser.add_argument(
        "--refresh", action="store_true", help="refetch dates already completed"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="recreate the requested historical output instead of resuming existing rows",
    )
    return parser.parse_args()


def parse_cli_date(value: str, field: str) -> date:
    if value.lower() == "today":
        return datetime.now(TAIPEI_TZ).date()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD or today") from exc


def number(value: Any) -> int | float:
    text = re.sub(r"<[^>]+>", "", str(value)).replace(",", "").strip()
    if not text or text in {"--", "---", "-"}:
        raise SourceError(f"missing numeric value: {value}")
    try:
        result = round(float(text), 6)
    except ValueError as exc:
        raise SourceError(f"invalid numeric value: {value}") from exc
    return int(result) if result.is_integer() else result


def integer(value: Any) -> int:
    result = number(value)
    if not isinstance(result, int):
        raise SourceError(f"expected integer: {value}")
    return result


def table_by_title(payload: Mapping[str, Any], phrase: str) -> Mapping[str, Any]:
    for table in payload.get("tables", []):
        if isinstance(table, dict) and phrase in str(table.get("title", "")):
            return table
    raise SourceError(f"missing table: {phrase}")


def row_by_label(table: Mapping[str, Any], label: str) -> list[Any]:
    for row in table.get("data", []):
        if isinstance(row, list) and row and str(row[0]).strip() == label:
            return row
    raise SourceError(f"missing row: {label}")


def fetch_tpex_month(
    http: RateLimitedSession, year: int, month: int
) -> dict[str, dict[str, Any]]:
    payload = http.get_json(
        TPEX_MONTHLY_URL,
        params={"date": f"{year:04d}/{month:02d}/01", "response": "json"},
    )
    if not isinstance(payload, dict) or payload.get("stat") != "ok":
        raise SourceError("TPEx monthly index response unavailable")
    table = table_by_title(payload, "日成交量值指數")
    rows: dict[str, dict[str, Any]] = {}
    for item in table.get("data", []):
        if not isinstance(item, list) or len(item) < 6:
            continue
        try:
            roc_year, row_month, row_day = (int(part) for part in str(item[0]).split("/"))
            trade_date = date(roc_year + 1911, row_month, row_day).isoformat()
            close = number(item[4])
            change = number(item[5])
            previous = close - change
            if previous <= 0:
                raise SourceError("invalid TPEx previous close")
            rows[trade_date] = {
                "close": close,
                "change_percent": round(change / previous * 100, 6),
            }
        except (ValueError, SourceError):
            continue
    return rows


def load_taiwan_trading_days(
    http: RateLimitedSession, start: date, end: date
) -> tuple[list[date], dict[str, dict[str, Any]]]:
    months: list[tuple[int, int]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        months.append((cursor.year, cursor.month))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    tpex: dict[str, dict[str, Any]] = {}
    for year, month in months:
        try:
            tpex.update(fetch_tpex_month(http, year, month))
        except SourceError as exc:
            LOGGER.warning("TPEx calendar month unavailable %04d-%02d: %s", year, month, exc)
    days = sorted(
        datetime.strptime(day, "%Y-%m-%d").date()
        for day in tpex
        if start <= datetime.strptime(day, "%Y-%m-%d").date() <= end
    )
    if not days:
        raise SourceError("no official TPEx trading dates found in requested range")
    return days, tpex


def fetch_twse_market(http: RateLimitedSession, trade_date: date) -> dict[str, Any]:
    day = trade_date.strftime("%Y%m%d")
    payload = http.get_json(
        TWSE_DAILY_URL,
        params={"date": day, "type": "ALLBUT0999", "response": "json"},
    )
    if not isinstance(payload, dict) or payload.get("stat") != "OK" or payload.get("date") != day:
        raise SourceError("TWSE daily market response unavailable or date mismatch")
    index_table = table_by_title(payload, "價格指數(臺灣證券交易所)")
    taiex = row_by_label(index_table, "發行量加權股價指數")
    stats = table_by_title(payload, "大盤統計資訊")
    turnover = row_by_label(stats, "1.一般股票")
    breadth = table_by_title(payload, "漲跌證券數合計")
    fields = breadth.get("fields", [])
    if "股票" not in fields:
        raise SourceError("TWSE breadth stock column missing")
    stock_column = fields.index("股票")
    values: dict[str, int] = {}
    labels = {"上漲(漲停)": "advancing", "下跌(跌停)": "declining", "持平": "unchanged"}
    for label, key in labels.items():
        row = row_by_label(breadth, label)
        values[key] = integer(str(row[stock_column]).split("(", 1)[0])
    return {
        "close": number(taiex[1]),
        "change_percent": number(taiex[4]),
        "turnover": integer(turnover[1]),
        **values,
    }


def fetch_tpex_quotes(http: RateLimitedSession, trade_date: date) -> dict[str, Any]:
    payload = http.get_json(
        TPEX_QUOTES_URL,
        params={"date": trade_date.strftime("%Y/%m/%d"), "response": "json"},
    )
    expected = trade_date.strftime("%Y%m%d")
    if not isinstance(payload, dict) or payload.get("date") != expected:
        raise SourceError("TPEx daily quote response unavailable or date mismatch")
    table = table_by_title(payload, "上櫃股票行情")
    fields = table.get("fields", [])
    if "代號" not in fields or "漲跌" not in fields:
        raise SourceError("TPEx quote fields changed")
    code_index, change_index = fields.index("代號"), fields.index("漲跌")
    counts = {"advancing": 0, "declining": 0, "unchanged": 0}
    for row in table.get("data", []):
        if not isinstance(row, list) or len(row) <= max(code_index, change_index):
            continue
        if not re.fullmatch(r"\d{4}", str(row[code_index]).strip()):
            continue
        change_text = str(row[change_index]).replace(",", "").strip()
        if not change_text or change_text in {"---", "--", "-"}:
            continue
        try:
            change = float(change_text)
        except ValueError:
            continue
        counts["advancing" if change > 0 else "declining" if change < 0 else "unchanged"] += 1
    return {"turnover": integer(table.get("totalTradingAmount")), **counts}


def fetch_foreign_cash(http: RateLimitedSession, trade_date: date) -> int:
    day = trade_date.strftime("%Y%m%d")
    payload = http.get_json(
        TWSE_INSTITUTIONAL_URL,
        params={"dayDate": day, "response": "json"},
    )
    if not isinstance(payload, dict) or payload.get("stat") != "OK" or payload.get("date") != day:
        raise SourceError("TWSE institutional response unavailable or date mismatch")
    fields = payload.get("fields", [])
    rows = payload.get("data", [])
    if "買賣差額" not in fields:
        raise SourceError("TWSE institutional net field missing")
    net_index = fields.index("買賣差額")
    total = 0
    found = False
    for row in rows:
        if isinstance(row, list) and row and str(row[0]).startswith("外資及陸資"):
            total += integer(row[net_index])
            found = True
    if not found:
        raise SourceError("TWSE foreign investor row missing")
    return total


def fetch_foreign_futures(http: RateLimitedSession, trade_date: date) -> int:
    response = http.request(
        "POST",
        TAIFEX_POSITION_URL,
        data={
            "queryType": "1",
            "goDay": "",
            "doQuery": "1",
            "dateaddcnt": "",
            "queryDate": trade_date.strftime("%Y/%m/%d"),
            "commodityId": "TXF",
        },
    )
    response.encoding = "utf-8"
    parser = _TableParser()
    parser.feed(response.text)
    provider = TaifexProvider()
    parsed_date = provider._extract_trade_date(parser.visible_text)
    if parsed_date != trade_date.isoformat():
        raise SourceError("TAIFEX foreign position unavailable or date mismatch")
    records = provider.normalize({"trade_date": parsed_date, "rows": parser.rows})
    if not provider.validate(records):
        raise SourceError("TAIFEX foreign position validation failed")
    return integer(records[0]["net_position"]["open_interest"])


def fetch_night_futures(http: RateLimitedSession, trade_date: date) -> int | float:
    response = http.request(
        "POST",
        TAIFEX_NIGHT_URL,
        data={
            "queryType": "2",
            "marketCode": "1",
            "MarketCode": "1",
            "dateaddcnt": "",
            "commodity_id": "TX",
            "commodity_id2": "",
            "queryDate": trade_date.strftime("%Y/%m/%d"),
        },
    )
    response.encoding = "utf-8"
    parser = _NightMarketParser()
    parser.feed(response.text)
    provider = TaifexNightFuturesProvider()
    if parser.query_date != trade_date.strftime("%Y/%m/%d"):
        raise SourceError("TAIFEX night response date mismatch")
    records = provider.normalize({"trade_date": trade_date.isoformat(), "rows": parser.rows})
    if not provider.validate(records) or records[0].get("change") is None:
        raise SourceError("TAIFEX night futures validation failed")
    return number(records[0]["change"])


def fetch_yahoo_history(
    http: RateLimitedSession, symbol: str, start: date, end: date
) -> dict[date, dict[str, Any]]:
    period_start = int(datetime.combine(start - timedelta(days=10), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    period_end = int(datetime.combine(end + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    payload = http.get_json(
        YAHOO_CHART_URL.format(symbol=requests.utils.quote(symbol, safe="")),
        params={
            "period1": period_start,
            "period2": period_end,
            "interval": "1d",
            "events": "history",
        },
    )
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quotes = result["indicators"]["quote"][0]
        closes = quotes["close"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SourceError(f"Yahoo {symbol} response format changed") from exc
    rows: dict[date, dict[str, Any]] = {}
    previous_close: float | None = None
    for timestamp, close_value in zip(timestamps, closes):
        if close_value is None:
            continue
        market_date = datetime.fromtimestamp(timestamp, timezone.utc).date()
        close = float(close_value)
        if previous_close is not None and previous_close > 0:
            rows[market_date] = {
                "close": round(close, 6),
                "change_percent": round((close / previous_close - 1) * 100, 6),
            }
        previous_close = close
    return rows


def latest_on_or_before(
    history: Mapping[date, Mapping[str, Any]], allowed_date: date, source: str
) -> tuple[date, Mapping[str, Any]]:
    dates = sorted(history)
    index = bisect_right(dates, allowed_date) - 1
    if index < 0:
        raise SourceError(f"{source} has no completed session on or before {allowed_date}")
    actual_date = dates[index]
    return actual_date, history[actual_date]


def latest_before(
    history: Mapping[date, Mapping[str, Any]], cutoff_date: date, source: str
) -> tuple[date, Mapping[str, Any]]:
    """Return the latest completed US session strictly before Taiwan target day."""

    dates = sorted(history)
    index = bisect_right(dates, cutoff_date - timedelta(days=1)) - 1
    if index < 0:
        raise SourceError(f"{source} has no completed session before {cutoff_date}")
    actual_date = dates[index]
    return actual_date, history[actual_date]


def load_rows(path: Path, fields: Iterable[str], key: str) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(fields):
            raise ValueError(f"Unexpected columns in {path}")
        rows: dict[str, dict[str, str]] = {}
        for row in reader:
            row_key = row.get(key, "")
            if not row_key or row_key in rows:
                raise ValueError(f"Missing or duplicate {key} in {path}")
            rows[row_key] = row
        return rows


def atomic_write(path: Path, fields: Iterable[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_checkpoint(
    completed: Mapping[str, Mapping[str, Any]],
    failures: Mapping[str, Mapping[str, Any]],
) -> None:
    atomic_write(OUTPUT_PATH, MARKET_FIELDS, (completed[key] for key in sorted(completed)))
    atomic_write(
        FAILURE_PATH,
        FAILURE_FIELDS,
        (failures[key] for key in sorted(failures, key=lambda item: tuple(item.split("|", 1)))),
    )


def record_failure(
    failures: dict[str, dict[str, Any]], trade_date: date, source: str, exc: Exception
) -> None:
    key = f"{trade_date.isoformat()}|{source}"
    failures[key] = {
        "trade_date": trade_date.isoformat(),
        "source": source,
        "error": str(exc).replace("\r", " ").replace("\n", " ")[:1000],
        "attempted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_row(
    http: RateLimitedSession,
    trade_date: date,
    target_date: date,
    tpex_index: Mapping[str, Any],
    yahoo: Mapping[str, Mapping[date, Mapping[str, Any]]],
) -> tuple[dict[str, Any] | None, dict[str, Exception]]:
    today = datetime.now(TAIPEI_TZ).date()
    try:
        taifex_public_start = today.replace(year=today.year - 3)
    except ValueError:
        taifex_public_start = today.replace(year=today.year - 3, day=28)
    if trade_date < taifex_public_start:
        return None, {
            "taifex_foreign_position": SourceError(
                "TAIFEX public daily institutional query is limited to the previous three years"
            )
        }

    failures: dict[str, Exception] = {}
    values: dict[str, Any] = {}
    # TAIFEX is the narrowest/most rate-limited source. Check it first so a
    # missing observation does not trigger four requests for a row that cannot
    # be complete anyway.
    try:
        values["taifex_foreign_position"] = fetch_foreign_futures(http, trade_date)
    except Exception as exc:
        return None, {"taifex_foreign_position": exc}

    fetchers: dict[str, Callable[[], Any]] = {
        "taifex_night": lambda: fetch_night_futures(http, target_date),
        "twse_market": lambda: fetch_twse_market(http, trade_date),
        "tpex_market": lambda: fetch_tpex_quotes(http, trade_date),
        "twse_institutional": lambda: fetch_foreign_cash(http, trade_date),
    }
    for source, fetcher in fetchers.items():
        try:
            values[source] = fetcher()
        except Exception as exc:  # isolate every provider/date
            failures[source] = exc
    us_values: dict[str, tuple[date, Mapping[str, Any]]] = {}
    for source in YAHOO_SYMBOLS:
        try:
            us_values[source] = latest_before(yahoo[source], target_date, source)
        except Exception as exc:
            failures[f"yahoo_{source}"] = exc
    if failures:
        return None, failures

    twse = values["twse_market"]
    tpex_market = values["tpex_market"]
    row = {
        "trade_date": trade_date.isoformat(),
        "prediction_target_date": target_date.isoformat(),
        "taiwan_market_trade_date": trade_date.isoformat(),
        "institutional_trade_date": trade_date.isoformat(),
        "foreign_futures_trade_date": trade_date.isoformat(),
        # TAIFEX attributes T-close-to-T+1-05:00 after-hours data to T+1.
        "night_futures_trade_date": target_date.isoformat(),
        "tsm_adr_trade_date": us_values["tsm_adr"][0].isoformat(),
        "sox_trade_date": us_values["sox"][0].isoformat(),
        "sp500_trade_date": us_values["sp500"][0].isoformat(),
        "nasdaq_trade_date": us_values["nasdaq"][0].isoformat(),
        "nikkei_trade_date": us_values["nikkei"][0].isoformat(),
        "kospi_trade_date": us_values["kospi"][0].isoformat(),
        "hang_seng_trade_date": us_values["hang_seng"][0].isoformat(),
        "csi300_trade_date": us_values["csi300"][0].isoformat(),
        "soxx_trade_date": us_values["soxx"][0].isoformat(),
        "smh_trade_date": us_values["smh"][0].isoformat(),
        "nvda_trade_date": us_values["nvda"][0].isoformat(),
        "amd_trade_date": us_values["amd"][0].isoformat(),
        "avgo_trade_date": us_values["avgo"][0].isoformat(),
        "taiex_close": twse["close"],
        "taiex_change_percent": twse["change_percent"],
        "tpex_close": tpex_index["close"],
        "turnover": twse["turnover"] + tpex_market["turnover"],
        "advancing": twse["advancing"] + tpex_market["advancing"],
        "declining": twse["declining"] + tpex_market["declining"],
        "unchanged": twse["unchanged"] + tpex_market["unchanged"],
        "foreign_cash_flow": values["twse_institutional"],
        "foreign_futures_position": values["taifex_foreign_position"],
        "night_futures_change": values["taifex_night"],
        "tsm_adr_change_percent": us_values["tsm_adr"][1]["change_percent"],
        "sox_change_percent": us_values["sox"][1]["change_percent"],
        "sp500_change_percent": us_values["sp500"][1]["change_percent"],
        "nasdaq_change_percent": us_values["nasdaq"][1]["change_percent"],
        "nikkei_change_percent": us_values["nikkei"][1]["change_percent"],
        "kospi_change_percent": us_values["kospi"][1]["change_percent"],
        "hang_seng_change_percent": us_values["hang_seng"][1]["change_percent"],
        "csi300_change_percent": us_values["csi300"][1]["change_percent"],
        "soxx_change_percent": us_values["soxx"][1]["change_percent"],
        "smh_change_percent": us_values["smh"][1]["change_percent"],
        "nvda_change_percent": us_values["nvda"][1]["change_percent"],
        "amd_change_percent": us_values["amd"][1]["change_percent"],
        "avgo_change_percent": us_values["avgo"][1]["change_percent"],
    }
    if any(row[field] in (None, "") for field in MARKET_FIELDS):
        return None, {"validation": SourceError("row contains missing values")}
    for source_field in (
        "tsm_adr_trade_date",
        "sox_trade_date",
        "sp500_trade_date",
        "nasdaq_trade_date",
        "nikkei_trade_date",
        "kospi_trade_date",
        "hang_seng_trade_date",
        "csi300_trade_date",
        "soxx_trade_date",
        "smh_trade_date",
        "nvda_trade_date",
        "amd_trade_date",
        "avgo_trade_date",
    ):
        source_date = date.fromisoformat(str(row[source_field]))
        if source_date >= target_date:
            return None, {
                "leakage_validation": SourceError(
                    f"{source_field} is not completed before target_date"
                )
            }
    if date.fromisoformat(str(row["night_futures_trade_date"])) != target_date:
        return None, {
            "leakage_validation": SourceError(
                "night_futures_trade_date must equal prediction_target_date"
            )
        }
    return row, {}


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    start, end = parse_cli_date(args.start, "--start"), parse_cli_date(args.end, "--end")
    if start > end:
        raise ValueError("--start must not be later than --end")
    http = RateLimitedSession(args.delay, args.retries, args.timeout)
    completed = (
        {} if args.rebuild else load_rows(OUTPUT_PATH, MARKET_FIELDS, "trade_date")
    )
    failure_rows: dict[str, dict[str, Any]] = {}
    if FAILURE_PATH.exists():
        with FAILURE_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != list(FAILURE_FIELDS):
                raise ValueError(f"Unexpected columns in {FAILURE_PATH}")
            for item in reader:
                failure_rows[f"{item['trade_date']}|{item['source']}"] = item

    LOGGER.info("Loading official Taiwan trading calendar %s to %s", start, end)
    trading_days, tpex_history = load_taiwan_trading_days(http, start, end)
    LOGGER.info("Official Taiwan trading dates: %d", len(trading_days))
    yahoo: dict[str, dict[date, dict[str, Any]]] = {}
    for source, symbol in YAHOO_SYMBOLS.items():
        yahoo[source] = fetch_yahoo_history(http, symbol, start, end)
        LOGGER.info("Yahoo completed sessions loaded: %s=%d", source, len(yahoo[source]))

    processed = 0
    succeeded = 0
    failed_dates = 0
    feature_days = trading_days[:-1]
    for position, trade_date in enumerate(feature_days, 1):
        target_date = trading_days[position]
        key = trade_date.isoformat()
        if key in completed and not args.refresh:
            continue
        LOGGER.info(
            "[%d/%d] Backfilling %s -> %s",
            position,
            len(feature_days),
            key,
            target_date,
        )
        row, failures = build_row(
            http, trade_date, target_date, tpex_history[key], yahoo
        )
        if row is not None:
            completed[key] = row
            succeeded += 1
            for failure_key in [item for item in failure_rows if item.startswith(f"{key}|")]:
                del failure_rows[failure_key]
        else:
            failed_dates += 1
            for source, exc in failures.items():
                LOGGER.warning("%s | %s | %s", key, source, exc)
                record_failure(failure_rows, trade_date, source, exc)
        processed += 1
        if processed % max(args.checkpoint, 1) == 0:
            write_checkpoint(completed, failure_rows)
    write_checkpoint(completed, failure_rows)
    duration = time.monotonic() - started
    LOGGER.info(
        "Backfill finished | trading_days=%d processed=%d new_or_updated=%d "
        "failed_dates=%d completed_total=%d failure_items=%d duration=%.2fs",
        len(trading_days), processed, succeeded, failed_dates, len(completed),
        len(failure_rows), duration,
    )
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        return run(parse_args())
    except KeyboardInterrupt:
        LOGGER.warning("Backfill interrupted; the last atomic checkpoint is preserved")
        return 130
    except Exception as exc:
        LOGGER.error("Backfill could not start or finish: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
