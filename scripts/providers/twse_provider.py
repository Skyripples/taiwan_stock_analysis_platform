"""TWSE institutional investors market data provider."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import requests

from .base_provider import BaseProvider, NormalizedRecords


class TwseProvider(BaseProvider):
    """Fetch the latest available institutional investor trading totals."""

    name = "TWSE"
    enabled = True
    dataset = "institutional_investors"
    output_filename = "institutional_investors.json"

    source_url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
    request_timeout_seconds = 30

    def fetch(self) -> Any:
        response = requests.get(
            self.source_url,
            params={"response": "json"},
            headers={
                "Accept": "application/json",
                "User-Agent": "taiwan-stock-analysis-platform/1.0",
            },
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()

        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("TWSE returned an unexpected response type")
        if payload.get("stat") != "OK":
            message = payload.get("stat") or payload.get("message") or "unknown status"
            raise ValueError(f"TWSE returned no available data: {message}")
        if not payload.get("date") or not payload.get("data"):
            raise ValueError("TWSE returned an empty institutional investor dataset")
        return payload

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        if not isinstance(raw_data, dict):
            raise TypeError("TWSE raw data must be an object")

        fields = raw_data.get("fields")
        rows = raw_data.get("data")
        if not isinstance(fields, list) or not isinstance(rows, list):
            raise ValueError("TWSE response is missing fields or data rows")

        required_fields = ("單位名稱", "買進金額", "賣出金額", "買賣差額")
        try:
            field_indexes = {field: fields.index(field) for field in required_fields}
        except ValueError as exc:
            raise ValueError("TWSE response fields do not match the expected format") from exc

        indexed_rows: Dict[str, Dict[str, int]] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) < len(fields):
                continue
            label = str(row[field_indexes["單位名稱"]]).strip()
            indexed_rows[label] = {
                "buy": self._parse_amount(row[field_indexes["買進金額"]]),
                "sell": self._parse_amount(row[field_indexes["賣出金額"]]),
                "net": self._parse_amount(row[field_indexes["買賣差額"]]),
            }

        dealer_proprietary = self._require_row(indexed_rows, "自營商(自行買賣)")
        dealer_hedging = self._require_row(indexed_rows, "自營商(避險)")
        investment_trust = self._require_row(indexed_rows, "投信")
        foreign = self._find_foreign_row(indexed_rows)
        total = self._require_row(indexed_rows, "合計")

        dealers = {
            "buy": dealer_proprietary["buy"] + dealer_hedging["buy"],
            "sell": dealer_proprietary["sell"] + dealer_hedging["sell"],
            "net": dealer_proprietary["net"] + dealer_hedging["net"],
            "breakdown": {
                "proprietary": dealer_proprietary,
                "hedging": dealer_hedging,
            },
        }

        trade_date = datetime.strptime(str(raw_data["date"]), "%Y%m%d").date().isoformat()
        return [
            {
                "trade_date": trade_date,
                "metadata": {
                    "market": "TWSE",
                    "currency": "TWD",
                    "amount_unit": "元",
                    "source": self.source_url,
                    "foreign_scope": "外資及陸資（不含外資自營商）",
                },
                "foreign_and_mainland_investors": foreign,
                "investment_trust": investment_trust,
                "dealers": dealers,
                "total": total,
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
            if metadata.get("currency") != "TWD" or metadata.get("amount_unit") != "元":
                return False

            groups = (
                record["foreign_and_mainland_investors"],
                record["investment_trust"],
                record["dealers"],
                record["total"],
            )
            if not all(self._valid_amount_group(group) for group in groups):
                return False

            dealer_breakdown = record["dealers"]["breakdown"]
            if not all(
                self._valid_amount_group(dealer_breakdown[key])
                for key in ("proprietary", "hedging")
            ):
                return False

            for field in ("buy", "sell", "net"):
                dealer_sum = (
                    dealer_breakdown["proprietary"][field]
                    + dealer_breakdown["hedging"][field]
                )
                if record["dealers"][field] != dealer_sum:
                    return False

                institutional_sum = (
                    record["foreign_and_mainland_investors"][field]
                    + record["investment_trust"][field]
                    + record["dealers"][field]
                )
                if record["total"][field] != institutional_sum:
                    return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _parse_amount(value: Any) -> int:
        text = str(value).replace(",", "").strip()
        if not text:
            raise ValueError("TWSE amount is empty")
        return int(text)

    @staticmethod
    def _require_row(rows: Dict[str, Dict[str, int]], label: str) -> Dict[str, int]:
        try:
            return rows[label]
        except KeyError as exc:
            raise ValueError(f"TWSE response is missing row: {label}") from exc

    @staticmethod
    def _find_foreign_row(rows: Dict[str, Dict[str, int]]) -> Dict[str, int]:
        for label, values in rows.items():
            if label.startswith("外資及陸資"):
                return values
        raise ValueError("TWSE response is missing the foreign investor row")

    @staticmethod
    def _valid_amount_group(group: Any) -> bool:
        if not isinstance(group, dict):
            return False
        if not all(isinstance(group.get(field), int) for field in ("buy", "sell", "net")):
            return False
        return group["buy"] - group["sell"] == group["net"]
