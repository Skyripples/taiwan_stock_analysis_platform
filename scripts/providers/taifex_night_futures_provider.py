"""TAIFEX Taiwan futures after-hours market provider."""

from __future__ import annotations

import re
from calendar import monthcalendar
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List

import requests

from .base_provider import BaseProvider, NormalizedRecords


TAIPEI_TZ = timezone(timedelta(hours=8))
Number = int | float


class _NightMarketParser(HTMLParser):
    """Parse structured table rows and query metadata from TAIFEX HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self.query_date = ""
        self.text_parts: List[str] = []
        self._in_row = False
        self._in_cell = False
        self._row: List[str] = []
        self._cell_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "queryDate":
            self.query_date = str(attributes.get("value", "")).strip()
        elif tag == "tr":
            self._in_row = True
            self._row = []
        elif self._in_row and tag in {"td", "th"}:
            self._in_cell = True
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.text_parts.append(text)
        if self._in_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._in_row and tag in {"td", "th"}:
            cell = re.sub(r"\s+", " ", " ".join(self._cell_parts)).strip()
            self._row.append(cell)
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if self._row:
                self.rows.append(self._row)
            self._in_row = False

    @property
    def visible_text(self) -> str:
        return " ".join(self.text_parts)


class TaifexNightFuturesProvider(BaseProvider):
    """Fetch the nearest unexpired TX contract's latest after-hours session."""

    name = "TAIFEX"
    registry_name = "TAIFEX:night_futures"
    dataset = "night_futures"
    output_filename = "night_futures.json"
    enabled = True

    source_url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
    product_name = "臺股期貨"
    product_code = "TX"
    session_name = "after_hours"
    request_timeout_seconds = 30
    lookback_days = 14

    REQUIRED_HEADERS = {
        "product": "契約",
        "contract_month": "到期月份(週別)",
        "open": "開盤價",
        "high": "最高價",
        "low": "最低價",
        "close": "最後成交價",
        "change": "漲跌價",
        "change_percent": "漲跌%",
        "volume": "成交量",
    }

    def fetch(self) -> Any:
        today_taipei = datetime.now(TAIPEI_TZ).date()
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-TW,zh;q=0.9",
                "User-Agent": "taiwan-stock-analysis-platform/1.0",
            }
        )

        for days_ago in range(self.lookback_days + 1):
            query_date = today_taipei - timedelta(days=days_ago)
            query_date_text = query_date.strftime("%Y/%m/%d")
            response = session.post(
                self.source_url,
                data={
                    "queryType": "2",
                    "marketCode": "1",
                    "MarketCode": "1",
                    "dateaddcnt": "",
                    "commodity_id": self.product_code,
                    "commodity_id2": "",
                    "queryDate": query_date_text,
                },
                timeout=self.request_timeout_seconds,
            )
            response.raise_for_status()
            response.encoding = "utf-8"

            parser = _NightMarketParser()
            parser.feed(response.text)
            if parser.query_date != query_date_text:
                continue
            if "盤後交易時段行情表" not in parser.visible_text:
                continue
            if not self._contains_tx_market_rows(parser.rows):
                continue

            return {
                "trade_date": query_date.isoformat(),
                "rows": parser.rows,
            }

        raise ValueError(
            f"TAIFEX returned no {self.product_name} after-hours data "
            f"in the last {self.lookback_days + 1} days"
        )

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        if not isinstance(raw_data, dict):
            raise TypeError("TAIFEX night market raw data must be an object")
        trade_date_text = raw_data.get("trade_date")
        rows = raw_data.get("rows")
        if not isinstance(trade_date_text, str) or not isinstance(rows, list):
            raise ValueError("TAIFEX night market data is missing trade date or rows")

        trade_date = datetime.strptime(trade_date_text, "%Y-%m-%d").date()
        header_map, data_rows = self._map_market_table(rows)
        candidates = [self._row_to_mapping(row, header_map) for row in data_rows]
        contract = self._select_nearest_contract(candidates, trade_date)

        return [
            {
                "trade_date": trade_date.isoformat(),
                "metadata": {
                    "market": "TAIFEX",
                    "product_name": self.product_name,
                    "product_code": self.product_code,
                    "session": self.session_name,
                    "contract_month": contract["contract_month"],
                    "currency": "TWD",
                    "price_unit": "point",
                    "volume_unit": "contract",
                    "change_basis": "previous_regular_session_settlement_price",
                    "source": self.source_url,
                },
                "open": self._parse_number(contract["open"]),
                "high": self._parse_number(contract["high"]),
                "low": self._parse_number(contract["low"]),
                "close": self._parse_number(contract["close"]),
                "change": self._parse_number(contract["change"], allow_null=True),
                "change_percent": self._parse_number(
                    contract["change_percent"], allow_null=True
                ),
                "volume": self._parse_integer(contract["volume"]),
            }
        ]

    def validate(self, records: NormalizedRecords) -> bool:
        if not isinstance(records, list) or len(records) != 1:
            return False
        record = records[0]
        if not isinstance(record, dict):
            return False

        try:
            datetime.strptime(record["trade_date"], "%Y-%m-%d")
            metadata = record["metadata"]
            if (
                metadata.get("market") != "TAIFEX"
                or metadata.get("product_code") != self.product_code
                or metadata.get("session") != self.session_name
                or not re.fullmatch(r"\d{6}", str(metadata.get("contract_month", "")))
                or metadata.get("price_unit") != "point"
            ):
                return False

            open_price = record["open"]
            high_price = record["high"]
            low_price = record["low"]
            close_price = record["close"]
            if not all(self._is_number(value) for value in (open_price, high_price, low_price, close_price)):
                return False
            if high_price < low_price or high_price < max(open_price, close_price):
                return False
            if low_price > min(open_price, close_price):
                return False

            volume = record["volume"]
            if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
                return False
            for field in ("change", "change_percent"):
                value = record[field]
                if value is not None and not self._is_number(value):
                    return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    @classmethod
    def _contains_tx_market_rows(cls, rows: List[List[str]]) -> bool:
        try:
            _, data_rows = cls._map_market_table(rows)
        except ValueError:
            return False
        return any(row and row[0].strip() == cls.product_code for row in data_rows)

    @classmethod
    def _map_market_table(
        cls, rows: List[List[str]]
    ) -> tuple[Dict[str, int], List[List[str]]]:
        normalized_required = {
            field: cls._normalize_header(header)
            for field, header in cls.REQUIRED_HEADERS.items()
        }
        for index, row in enumerate(rows):
            normalized_headers = [cls._normalize_header(cell) for cell in row]
            if not all(header in normalized_headers for header in normalized_required.values()):
                continue
            header_map = {
                field: normalized_headers.index(header)
                for field, header in normalized_required.items()
            }
            return header_map, rows[index + 1 :]
        raise ValueError("TAIFEX response is missing expected night market table headers")

    @staticmethod
    def _normalize_header(value: Any) -> str:
        return re.sub(r"[\s*]+", "", str(value)).strip()

    @staticmethod
    def _row_to_mapping(row: List[str], header_map: Dict[str, int]) -> Dict[str, str]:
        if not isinstance(row, list) or not header_map:
            return {}
        maximum_index = max(header_map.values())
        if len(row) <= maximum_index:
            return {}
        return {field: row[index].strip() for field, index in header_map.items()}

    @classmethod
    def _select_nearest_contract(
        cls, candidates: List[Dict[str, str]], trade_date: date
    ) -> Dict[str, str]:
        eligible: List[Dict[str, str]] = []
        for candidate in candidates:
            if candidate.get("product") != cls.product_code:
                continue
            contract_month = candidate.get("contract_month", "")
            if not re.fullmatch(r"\d{6}", contract_month):
                continue
            year = int(contract_month[:4])
            month = int(contract_month[4:])
            try:
                expiry_date = cls._third_wednesday(year, month)
            except (ValueError, IndexError):
                continue
            if expiry_date >= trade_date:
                eligible.append(candidate)

        if not eligible:
            raise ValueError("TAIFEX response has no unexpired monthly TX contract")
        return min(eligible, key=lambda item: item["contract_month"])

    @staticmethod
    def _third_wednesday(year: int, month: int) -> date:
        wednesdays = [week[2] for week in monthcalendar(year, month) if week[2] != 0]
        return date(year, month, wednesdays[2])

    @classmethod
    def _parse_number(cls, value: Any, *, allow_null: bool = False) -> Number | None:
        text = str(value).replace(",", "").strip()
        if text in {"", "-", "--"}:
            if allow_null:
                return None
            raise ValueError("TAIFEX numeric value is missing")

        is_negative = "▼" in text
        is_positive = "▲" in text
        numeric_text = re.sub(r"[^0-9.+-]", "", text)
        if not numeric_text:
            raise ValueError(f"Invalid TAIFEX numeric value: {value}")
        number = float(numeric_text)
        if is_negative:
            number = -abs(number)
        elif is_positive:
            number = abs(number)
        return int(number) if number.is_integer() else number

    @classmethod
    def _parse_integer(cls, value: Any) -> int:
        number = cls._parse_number(value)
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"TAIFEX integer value is invalid: {value}")
        return number

    @staticmethod
    def _is_number(value: Any) -> bool:
        return not isinstance(value, bool) and isinstance(value, (int, float))
