"""TWSE and TPEx daily market overview provider."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List

import requests

from .base_provider import BaseProvider, NormalizedRecords


class TaiwanMarketOverviewProvider(BaseProvider):
    """Fetch the latest common TWSE and TPEx completed trading day."""

    name = "TWSE/TPEx"
    dataset = "taiwan_market_overview"
    output_filename = "taiwan_market_overview.json"
    enabled = True

    twse_latest_url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
    twse_daily_url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    tpex_highlight_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainborad_highlight"
    request_timeout_seconds = 30

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

        tpex_rows = self._get_json(self.tpex_highlight_url)
        if not isinstance(tpex_rows, list) or not tpex_rows or not isinstance(tpex_rows[0], dict):
            raise ValueError("TPEx market highlight response is empty")
        tpex_date = self._parse_roc_date(tpex_rows[0].get("Date"))
        if twse_date != tpex_date:
            raise ValueError(
                f"TWSE and TPEx dates differ: {twse_date.isoformat()} / {tpex_date.isoformat()}"
            )

        return {
            "trade_date": twse_date.isoformat(),
            "twse": twse_daily,
            "tpex": tpex_rows[0],
        }

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        if not isinstance(raw_data, dict):
            raise TypeError("Taiwan market raw data must be an object")
        trade_date = raw_data.get("trade_date")
        twse = raw_data.get("twse")
        tpex = raw_data.get("tpex")
        if not isinstance(trade_date, str) or not isinstance(twse, dict) or not isinstance(tpex, dict):
            raise ValueError("Taiwan market response is missing required sections")

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
                        "tpex": self.tpex_highlight_url,
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
