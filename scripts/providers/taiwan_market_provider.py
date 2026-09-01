"""TWSE and TPEx daily market overview provider."""

from __future__ import annotations

import re
import logging
import time
from datetime import date, datetime
from typing import Any, Dict, Iterable, List

import requests

from .base_provider import BaseProvider, NormalizedRecords


LOGGER = logging.getLogger("market_data.taiwan_market")


class TaiwanMarketOverviewProvider(BaseProvider):
    """Fetch the latest common TWSE and TPEx completed trading day."""

    name = "TWSE/TPEx"
    dataset = "taiwan_market_overview"
    output_filename = "taiwan_market_overview.json"
    enabled = True

    twse_latest_url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
    twse_daily_url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    tpex_highlight_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainborad_highlight"
    tpex_index_url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingIndex"
    tpex_quotes_url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
    request_timeout_seconds = 30
    tpex_max_attempts = 3
    tpex_backoff_seconds = (1.0, 2.0)

    def fetch(self) -> Any:
        latest_rows = self._get_json(self.twse_latest_url)
        if not isinstance(latest_rows, list) or not latest_rows:
            raise ValueError("TWSE latest market index response is empty")

        taiex_latest = next(
            (
                row
                for row in latest_rows
                if isinstance(row, dict) and row.get("指數") == "發行量加權股價指數"
            ),
            None,
        )
        if not isinstance(taiex_latest, dict):
            raise ValueError("TWSE latest response is missing TAIEX")
        twse_date = self._parse_roc_date(taiex_latest.get("日期"))

        twse_daily = self._get_json(
            self.twse_daily_url,
            params={
                "date": twse_date.strftime("%Y%m%d"),
                "type": "ALLBUT0999",
                "response": "json",
            },
        )
        if not isinstance(twse_daily, dict) or twse_daily.get("stat") != "OK":
            raise ValueError("TWSE daily market overview is unavailable")
        LOGGER.info("Taiwan market sub-source completed: TWSE | trade_date=%s", twse_date)

        tpex_row, tpex_sources = self._fetch_tpex_for_date(twse_date)
        LOGGER.info(
            "Taiwan market sub-source completed: TPEx | trade_date=%s | source=%s",
            twse_date,
            ",".join(tpex_sources),
        )

        return {
            "trade_date": twse_date.isoformat(),
            "twse": twse_daily,
            "tpex": tpex_row,
            "tpex_sources": tpex_sources,
        }

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        if not isinstance(raw_data, dict):
            raise TypeError("Taiwan market raw data must be an object")
        trade_date = raw_data.get("trade_date")
        twse = raw_data.get("twse")
        tpex = raw_data.get("tpex")
        tpex_sources = raw_data.get("tpex_sources")
        if not isinstance(trade_date, str) or not isinstance(twse, dict) or not isinstance(tpex, dict):
            raise ValueError("Taiwan market response is missing required sections")
        if not isinstance(tpex_sources, list) or not tpex_sources:
            raise ValueError("Taiwan market response is missing TPEx source status")

        tables = twse.get("tables")
        if not isinstance(tables, list):
            raise ValueError("TWSE daily response is missing tables")

        taiex_row = self._find_table_row(tables, "發行量加權股價指數")
        turnover_row = self._find_table_row(tables, "總計(1~15)")
        breadth_table = self._find_table(tables, "漲跌證券數合計")
        breadth = self._parse_twse_breadth(breadth_table)

        taiex_close = self._parse_number(taiex_row[1])
        taiex_change = self._signed_change(taiex_row[2], taiex_row[3])
        taiex_change_percent = self._parse_number(taiex_row[4])

        tpex_close = self._parse_number(tpex.get("CloseIndex"))
        tpex_change = self._parse_number(tpex.get("IndexChange"))
        tpex_previous_close = tpex_close - tpex_change
        if tpex_previous_close <= 0:
            raise ValueError("TPEx previous close is invalid")
        tpex_change_percent = round((tpex_change / tpex_previous_close) * 100, 4)

        twse_turnover = self._parse_integer(turnover_row[1])
        tpex_turnover = self._parse_integer(tpex.get("DailyTradingValue")) * 1_000_000
        tpex_advancing = self._parse_integer(tpex.get("PriceRiseCompanyNumbers"))
        tpex_declining = self._parse_integer(tpex.get("PriceDeclineCompanyNumbers"))
        tpex_unchanged = self._parse_integer(tpex.get("PriceFlatCompanyNumbers"))

        return [
            {
                "trade_date": trade_date,
                "taiex": {
                    "close": taiex_close,
                    "change": taiex_change,
                    "change_percent": taiex_change_percent,
                },
                "tpex": {
                    "close": tpex_close,
                    "change": tpex_change,
                    "change_percent": tpex_change_percent,
                },
                "turnover": twse_turnover + tpex_turnover,
                "advancing": breadth["advancing"] + tpex_advancing,
                "declining": breadth["declining"] + tpex_declining,
                "unchanged": breadth["unchanged"] + tpex_unchanged,
                "metadata": {
                    "currency": "TWD",
                    "turnover_unit": "元",
                    "breadth_unit": "家",
                    "turnover_scope": "TWSE整體市場總計＋TPEx上櫃股票",
                    "breadth_scope": "TWSE股票＋TPEx上櫃公司",
                    "sources": {
                        "twse": self.twse_daily_url,
                        "tpex": tpex_sources,
                    },
                    "source_units": {
                        "twse_turnover": "元",
                        "tpex_turnover": "百萬元",
                    },
                },
            }
        ]

    def validate(self, records: NormalizedRecords) -> bool:
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            return False
        record = records[0]
        try:
            datetime.strptime(record["trade_date"], "%Y-%m-%d")
            for index_name in ("taiex", "tpex"):
                index = record[index_name]
                close = index["close"]
                change = index["change"]
                change_percent = index["change_percent"]
                if not all(self._is_number(value) for value in (close, change, change_percent)):
                    return False
                previous_close = close - change
                if close <= 0 or previous_close <= 0:
                    return False
                expected_percent = (change / previous_close) * 100
                if abs(expected_percent - change_percent) > 0.01:
                    return False

            if not isinstance(record["turnover"], int) or record["turnover"] < 0:
                return False
            if not all(
                isinstance(record[field], int) and record[field] >= 0
                for field in ("advancing", "declining", "unchanged")
            ):
                return False
            metadata = record["metadata"]
            if (
                metadata.get("currency") != "TWD"
                or metadata.get("turnover_unit") != "元"
                or metadata.get("breadth_unit") != "家"
                or not isinstance(metadata.get("sources"), dict)
            ):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    def _get_json(self, url: str, *, params: Dict[str, str] | None = None) -> Any:
        response = requests.get(
            url,
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "taiwan-stock-analysis-platform/1.0",
            },
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def _fetch_tpex_for_date(self, target_date: date) -> tuple[Dict[str, Any], List[str]]:
        """Return TPEx data for exactly target_date using official sources only."""

        primary_error: Exception | None = None
        try:
            rows = self._get_tpex_json(self.tpex_highlight_url)
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                raise ValueError("TPEx market highlight response is empty")
            row = rows[0]
            returned_date = self._parse_roc_date(row.get("Date"))
            if returned_date != target_date:
                raise ValueError(
                    "TPEx OpenAPI date mismatch: "
                    f"expected={target_date.isoformat()} returned={returned_date.isoformat()}"
                )
            return row, [self.tpex_highlight_url]
        except (requests.RequestException, ValueError, TypeError) as exc:
            primary_error = exc
            LOGGER.warning(
                "Taiwan market sub-source failed: TPEx primary | target_date=%s | %s",
                target_date,
                exc,
            )

        try:
            return self._fetch_tpex_fallback(target_date), [self.tpex_index_url, self.tpex_quotes_url]
        except (requests.RequestException, ValueError, TypeError) as fallback_error:
            LOGGER.error(
                "Taiwan market sub-source failed: TPEx fallback | target_date=%s | %s",
                target_date,
                fallback_error,
            )
            raise ValueError(
                "All official TPEx sources failed for "
                f"{target_date.isoformat()}: primary={primary_error}; fallback={fallback_error}"
            ) from fallback_error

    def _fetch_tpex_fallback(self, target_date: date) -> Dict[str, Any]:
        expected_date = target_date.strftime("%Y%m%d")
        params = {"date": target_date.strftime("%Y/%m/%d"), "response": "json"}
        index_payload = self._get_tpex_json(self.tpex_index_url, params=params)
        if (
            not isinstance(index_payload, dict)
            or index_payload.get("stat") != "ok"
            or index_payload.get("date") != expected_date
        ):
            raise ValueError("TPEx fallback index response is unavailable or date-mismatched")
        index_row = self._find_tpex_index_row(index_payload, target_date)

        quotes_payload = self._get_tpex_json(self.tpex_quotes_url, params=params)
        if (
            not isinstance(quotes_payload, dict)
            or quotes_payload.get("stat") != "ok"
            or quotes_payload.get("date") != expected_date
        ):
            raise ValueError("TPEx fallback quotes response is unavailable or date-mismatched")
        quote_table = self._find_tpex_quote_table(quotes_payload)
        counts = self._count_tpex_companies(quote_table)
        total_trading_amount = self._parse_integer(quote_table.get("totalTradingAmount"))

        close = self._parse_number(index_row[4])
        change = self._parse_number(index_row[5])
        return {
            "Date": f"{target_date.year - 1911:03d}{target_date:%m%d}",
            "DailyTradingValue": str(total_trading_amount // 1_000_000),
            "CloseIndex": str(close),
            "IndexChange": str(change),
            "PriceRiseCompanyNumbers": str(counts["advancing"]),
            "PriceDeclineCompanyNumbers": str(counts["declining"]),
            "PriceFlatCompanyNumbers": str(counts["unchanged"]),
        }

    def _get_tpex_json(self, url: str, *, params: Dict[str, str] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.tpex_max_attempts):
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "taiwan-stock-analysis-platform/1.0",
                    },
                    timeout=self.request_timeout_seconds,
                )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                status = (
                    exc.response.status_code
                    if isinstance(exc, requests.HTTPError) and exc.response is not None
                    else None
                )
                retryable = status is None or status == 429 or status >= 500
                if not retryable or attempt + 1 >= self.tpex_max_attempts:
                    raise
                delay = self.tpex_backoff_seconds[min(attempt, len(self.tpex_backoff_seconds) - 1)]
                LOGGER.warning(
                    "TPEx request retry: url=%s | attempt=%d/%d | delay=%.1fs | %s",
                    url,
                    attempt + 2,
                    self.tpex_max_attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
        raise RuntimeError(f"TPEx request failed without response: {url}: {last_error}")

    @classmethod
    def _find_tpex_index_row(cls, payload: Dict[str, Any], target_date: date) -> List[Any]:
        expected = target_date.strftime("%Y/%m/%d")
        for table in payload.get("tables", []):
            if not isinstance(table, dict):
                continue
            for row in table.get("data", []):
                if not isinstance(row, list) or len(row) < 6:
                    continue
                try:
                    if cls._parse_roc_date(row[0]) == target_date:
                        return row
                except ValueError:
                    continue
        raise ValueError(f"TPEx fallback index is missing target date: {expected}")

    @staticmethod
    def _find_tpex_quote_table(payload: Dict[str, Any]) -> Dict[str, Any]:
        for table in payload.get("tables", []):
            if (
                isinstance(table, dict)
                and table.get("totalTradingAmount") not in (None, "")
                and isinstance(table.get("data"), list)
            ):
                return table
        raise ValueError("TPEx fallback quote table is missing")

    @classmethod
    def _count_tpex_companies(cls, table: Dict[str, Any]) -> Dict[str, int]:
        counts = {"advancing": 0, "declining": 0, "unchanged": 0}
        for row in table.get("data", []):
            if not isinstance(row, list) or len(row) <= 3:
                continue
            code = str(row[0]).strip()
            if not re.fullmatch(r"[1-9]\d{3}", code):
                continue
            try:
                change = cls._parse_number(row[3])
            except (TypeError, ValueError):
                continue
            key = "advancing" if change > 0 else "declining" if change < 0 else "unchanged"
            counts[key] += 1
        if sum(counts.values()) == 0:
            raise ValueError("TPEx fallback company breadth is empty")
        return counts

    @staticmethod
    def _find_table(tables: Iterable[Any], title: str) -> Dict[str, Any]:
        for table in tables:
            if isinstance(table, dict) and table.get("title") == title:
                return table
        raise ValueError(f"TWSE response is missing table: {title}")

    @staticmethod
    def _find_table_row(tables: Iterable[Any], label: str) -> List[Any]:
        for table in tables:
            if not isinstance(table, dict):
                continue
            rows = table.get("data")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, list) and row and row[0] == label:
                    return row
        raise ValueError(f"TWSE response is missing row: {label}")

    @classmethod
    def _parse_twse_breadth(cls, table: Dict[str, Any]) -> Dict[str, int]:
        fields = table.get("fields")
        rows = table.get("data")
        if not isinstance(fields, list) or "股票" not in fields or not isinstance(rows, list):
            raise ValueError("TWSE breadth table format changed")
        stock_index = fields.index("股票")
        labels = {"上漲(漲停)": "advancing", "下跌(跌停)": "declining", "持平": "unchanged"}
        result: Dict[str, int] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) <= stock_index or row[0] not in labels:
                continue
            count_text = str(row[stock_index]).split("(", 1)[0]
            result[labels[row[0]]] = cls._parse_integer(count_text)
        if set(result) != set(labels.values()):
            raise ValueError("TWSE breadth rows are incomplete")
        return result

    @staticmethod
    def _signed_change(sign_html: Any, value: Any) -> int | float:
        change = TaiwanMarketOverviewProvider._parse_number(value)
        sign_text = re.sub(r"<[^>]+>", "", str(sign_html)).strip()
        if sign_text == "-":
            return -abs(change)
        if sign_text == "+":
            return abs(change)
        if change == 0:
            return 0
        raise ValueError("TWSE index change sign is invalid")

    @staticmethod
    def _parse_roc_date(value: Any) -> date:
        text = str(value).strip().replace("/", "")
        if len(text) != 7 or not text.isdigit():
            raise ValueError(f"Invalid ROC date: {value}")
        return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7]))

    @staticmethod
    def _parse_integer(value: Any) -> int:
        text = str(value).replace(",", "").strip()
        if not text or not re.fullmatch(r"-?\d+", text):
            raise ValueError(f"Invalid integer: {value}")
        return int(text)

    @staticmethod
    def _parse_number(value: Any) -> int | float:
        text = str(value).replace(",", "").strip()
        if not text:
            raise ValueError("Numeric value is empty")
        number = round(float(text), 4)
        return int(number) if number.is_integer() else number

    @staticmethod
    def _is_number(value: Any) -> bool:
        return not isinstance(value, bool) and isinstance(value, (int, float))
