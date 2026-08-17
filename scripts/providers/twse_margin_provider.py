"""TWSE market-wide margin financing and short selling balances."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from .base_provider import BaseProvider, NormalizedRecords


class TwseMarginProvider(BaseProvider):
    """Fetch the latest official TWSE credit trading statistics."""

    name = "TWSE"
    dataset = "margin_trading"
    output_filename = "margin_trading.json"
    enabled = True

    source_url = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
    page_url = "https://www.twse.com.tw/zh/trading/margin/mi-margn.html"
    request_timeout_seconds = 30

    def fetch(self) -> Any:
        response = requests.get(
            self.source_url,
            params={"response": "json", "selectType": "MS"},
            headers={
                "Accept": "application/json",
                "Accept-Language": "zh-TW,zh;q=0.9",
                "User-Agent": "taiwan-stock-analysis-platform/1.0",
            },
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("stat") != "OK":
            raise ValueError(f"TWSE margin trading returned no data: {payload.get('stat') if isinstance(payload, dict) else 'invalid response'}")
        return payload

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        if not isinstance(raw_data, dict):
            raise TypeError("TWSE margin raw data must be an object")
        tables = raw_data.get("tables")
        if not isinstance(tables, list):
            raise ValueError("TWSE margin response is missing tables")
        summary = next(
            (
                table for table in tables
                if isinstance(table, dict)
                and table.get("fields") == ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"]
                and isinstance(table.get("data"), list)
            ),
            None,
        )
        if summary is None:
            raise ValueError("TWSE credit trading summary table format changed")

        rows = {str(row[0]).strip(): row for row in summary["data"] if isinstance(row, list) and len(row) >= 6}
        financing_units = self._require_row(rows, "融資(交易單位)")
        short_units = self._require_row(rows, "融券(交易單位)")
        financing_amount = self._require_row(rows, "融資金額(仟元)")
        trade_date = datetime.strptime(str(raw_data.get("date")), "%Y%m%d").date().isoformat()

        return [{
            "trade_date": trade_date,
            "metadata": {
                "market": "TWSE",
                "currency": "TWD",
                "financing_amount_unit": "thousand_twd",
                "position_unit": "trading_unit",
                "source": self.source_url,
                "source_page": self.page_url,
                "balance_note": "TWSE publishes preliminary current balances; previous balances reflect final institution adjustments.",
            },
            "margin_financing": {
                "amount": self._balance_group(financing_amount),
                "trading_units": self._balance_group(financing_units),
            },
            "short_selling": {
                "trading_units": self._balance_group(short_units),
            },
        }]

    def validate(self, records: NormalizedRecords) -> bool:
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            return False
        record = records[0]
        try:
            datetime.strptime(record["trade_date"], "%Y-%m-%d")
            metadata = record["metadata"]
            if (
                metadata.get("currency") != "TWD"
                or metadata.get("financing_amount_unit") != "thousand_twd"
                or metadata.get("position_unit") != "trading_unit"
                or metadata.get("source") != self.source_url
            ):
                return False
            groups = (
                record["margin_financing"]["amount"],
                record["margin_financing"]["trading_units"],
                record["short_selling"]["trading_units"],
            )
            return all(self._valid_group(group) for group in groups)
        except (KeyError, TypeError, ValueError):
            return False

    @classmethod
    def _balance_group(cls, row: list[Any]) -> dict[str, int]:
        previous = cls._integer(row[4])
        current = cls._integer(row[5])
        return {
            "buy": cls._integer(row[1]),
            "sell": cls._integer(row[2]),
            "repayment": cls._integer(row[3]),
            "previous_balance": previous,
            "balance": current,
            "change": current - previous,
        }

    @staticmethod
    def _require_row(rows: dict[str, list[Any]], label: str) -> list[Any]:
        try:
            return rows[label]
        except KeyError as exc:
            raise ValueError(f"TWSE margin response is missing row: {label}") from exc

    @staticmethod
    def _integer(value: Any) -> int:
        text = str(value).replace(",", "").strip()
        if not text:
            raise ValueError("TWSE margin value is empty")
        return int(text)

    @staticmethod
    def _valid_group(group: Any) -> bool:
        fields = ("buy", "sell", "repayment", "previous_balance", "balance", "change")
        return (
            isinstance(group, dict)
            and all(isinstance(group.get(field), int) and group[field] >= 0 for field in fields if field != "change")
            and isinstance(group.get("change"), int)
            and group["balance"] - group["previous_balance"] == group["change"]
        )
