"""TAIFEX Taiwan futures institutional-position provider."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List

import requests

from .base_provider import BaseProvider, NormalizedRecords


TAIPEI_TZ = timezone(timedelta(hours=8))


class _TableParser(HTMLParser):
    """Extract table rows and visible text from TAIFEX HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self.text_parts: List[str] = []
        self._in_row = False
        self._in_cell = False
        self._row: List[str] = []
        self._cell_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        tag = tag.lower()
        if tag == "tr":
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


class TaifexProvider(BaseProvider):
    """Fetch the latest available foreign investor TX futures position."""

    name = "TAIFEX"
    dataset = "foreign_futures_position"
    output_filename = "foreign_futures_position.json"
    enabled = True

    source_url = "https://www.taifex.com.tw/cht/3/futContractsDate"
    product_name = "臺股期貨"
    product_code = "TXF"
    investor_name = "外資"
    investor_names = ("外資", "投信", "自營商")
    request_timeout_seconds = 30
    lookback_days = 14

    @staticmethod
    def _normalized_label(value: Any) -> str:
        return re.sub(r"\s+", "", str(value)).replace("台股期貨", "臺股期貨")

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-TW,zh;q=0.9",
                "User-Agent": "taiwan-stock-analysis-platform/1.0",
            }
        )
        return session

    def fetch_for_date(self, trade_date: date, *, session: requests.Session | None = None) -> Any | None:
        """Fetch one exact trade date; return ``None`` for an official empty/non-trading date."""
        client = session or self._session()
        response = client.post(
            self.source_url,
            data={
                "queryType": "1",
                "goDay": "",
                "doQuery": "1",
                "dateaddcnt": "",
                "queryDate": trade_date.strftime("%Y/%m/%d"),
                "commodityId": self.product_code,
            },
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        response.encoding = "utf-8"

        parser = _TableParser()
        parser.feed(response.text)
        parsed_trade_date = self._extract_trade_date(parser.visible_text)
        if parsed_trade_date != trade_date.isoformat():
            return None
        return {
            "trade_date": parsed_trade_date,
            "rows": parser.rows,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def fetch(self) -> Any:
        """Preserve the existing latest-available lookup over the last 14 days."""
        today_taipei = datetime.now(TAIPEI_TZ).date()
        session = self._session()

        for days_ago in range(self.lookback_days + 1):
            query_date = today_taipei - timedelta(days=days_ago)
            raw_data = self.fetch_for_date(query_date, session=session)
            if raw_data is not None:
                return raw_data

        raise ValueError(
            f"TAIFEX returned no {self.product_name} data in the last {self.lookback_days + 1} days"
        )

    def normalize_investors(
        self, raw_data: Any, investor_names: tuple[str, ...] | None = None,
    ) -> NormalizedRecords:
        """Normalize foreign, investment-trust, and dealer rows from one response."""
        if not isinstance(raw_data, dict):
            raise TypeError("TAIFEX raw data must be an object")
        rows = raw_data.get("rows")
        trade_date = raw_data.get("trade_date")
        if not isinstance(rows, list) or not isinstance(trade_date, str):
            raise ValueError("TAIFEX raw data is missing rows or trade date")

        wanted = investor_names or self.investor_names
        current_product = ""
        investor_values: Dict[str, List[str]] = {}

        for row in rows:
            if not isinstance(row, list):
                continue
            if len(row) >= 15 and row[0].strip().isdigit():
                current_product = self._normalized_label(row[1])
                investor = self._normalized_label(row[2])
                values = row[3:15]
            elif len(row) >= 13 and current_product:
                investor = self._normalized_label(row[0])
                values = row[1:13]
            else:
                continue

            if current_product != self._normalized_label(self.product_name):
                continue
            for investor_name in wanted:
                if investor.startswith(investor_name):
                    investor_values[investor_name] = values
                    break

        missing = [name for name in wanted if len(investor_values.get(name, [])) != 12]
        if missing:
            raise ValueError(f"TAIFEX response is missing TX futures rows: {', '.join(missing)}")

        records: NormalizedRecords = []
        for investor_name in wanted:
            values = investor_values[investor_name]
            records.append({
                "trade_date": trade_date,
                "metadata": {
                    "market": "TAIFEX",
                    "product_name": self.product_name,
                    "product_code": self.product_code,
                    "investor_type": investor_name,
                    "position_unit": "口",
                    "contract_amount_currency": "TWD",
                    "contract_amount_unit": "千元",
                    "source": self.source_url,
                },
                "long_position": {
                    "open_interest": self._parse_integer(values[6]),
                    "contract_amount": self._parse_integer(values[7]),
                },
                "short_position": {
                    "open_interest": self._parse_integer(values[8]),
                    "contract_amount": self._parse_integer(values[9]),
                },
                "net_position": {
                    "open_interest": self._parse_integer(values[10]),
                    "contract_amount": self._parse_integer(values[11]),
                },
            })
        return records

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        """Keep the legacy latest dataset shape restricted to foreign investors."""
        return self.normalize_investors(raw_data, (self.investor_name,))

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
                metadata.get("product_name") != self.product_name
                or metadata.get("product_code") != self.product_code
                or metadata.get("position_unit") != "口"
                or metadata.get("contract_amount_unit") != "千元"
            ):
                return False

            long_position = record["long_position"]
            short_position = record["short_position"]
            net_position = record["net_position"]
            for position in (long_position, short_position, net_position):
                if not all(
                    isinstance(position.get(field), int)
                    for field in ("open_interest", "contract_amount")
                ):
                    return False

            if long_position["open_interest"] - short_position["open_interest"] != net_position["open_interest"]:
                return False
            if long_position["contract_amount"] - short_position["contract_amount"] != net_position["contract_amount"]:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _extract_trade_date(visible_text: str) -> str | None:
        match = re.search(r"日期\s*[:：]?\s*(\d{4}/\d{2}/\d{2})", visible_text)
        if not match:
            return None
        try:
            return datetime.strptime(match.group(1), "%Y/%m/%d").date().isoformat()
        except ValueError:
            return None

    @staticmethod
    def _parse_integer(value: Any) -> int:
        text = str(value).replace(",", "").strip()
        if not text:
            raise ValueError("TAIFEX numeric value is empty")
        return int(text)
