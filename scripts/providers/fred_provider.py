"""Official Federal Reserve H.15 Treasury yield provider via FRED CSV."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

import requests

from .base_provider import BaseProvider, NormalizedRecords


class FredTreasuryProvider(BaseProvider):
    name = "FRED"
    dataset = "us_treasury_yields"
    output_filename = "us_treasury_yields.json"
    enabled = True
    source_url = "https://fred.stlouisfed.org/graph/fredgraph.csv"

    def fetch(self) -> Any:
        result = {}
        for series in ("DGS2", "DGS10"):
            response = requests.get(
                self.source_url,
                params={"id": series},
                headers={"User-Agent": "taiwan-stock-analysis-platform/1.0"},
                timeout=45,
            )
            response.raise_for_status()
            result[series] = response.text
        return result

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        histories = {series: self._parse_csv(raw_data.get(series), series) for series in ("DGS2", "DGS10")}
        common = sorted(set(histories["DGS2"]) & set(histories["DGS10"]))
        if len(common) < 2:
            raise ValueError("FRED yield histories have fewer than two common observations")
        current, previous = common[-1], common[-2]
        two, ten = histories["DGS2"][current], histories["DGS10"][current]
        return [{
            "trade_date": current.isoformat(),
            "observation_date": current.isoformat(),
            "us2y": two,
            "us10y": ten,
            "us2y_change": round(two - histories["DGS2"][previous], 6),
            "us10y_change": round(ten - histories["DGS10"][previous], 6),
            "yield_curve_10y_2y": round(ten - two, 6),
            "metadata": {
                "series": {"us2y": "DGS2", "us10y": "DGS10"},
                "unit": "percent; changes and spread are percentage points",
                "source": self.source_url,
                "original_source": "Board of Governors of the Federal Reserve System, H.15",
            },
        }]

    def validate(self, records: NormalizedRecords) -> bool:
        if not isinstance(records, list) or len(records) != 1:
            return False
        row = records[0]
        try:
            date.fromisoformat(row["observation_date"])
            values = [row[key] for key in ("us2y", "us10y", "us2y_change", "us10y_change", "yield_curve_10y_2y")]
            return all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values) and abs(row["yield_curve_10y_2y"] - (row["us10y"] - row["us2y"])) < 1e-6
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _parse_csv(text: Any, series: str) -> dict[date, float]:
        if not isinstance(text, str):
            raise ValueError(f"FRED {series} response is missing")
        history = {}
        for row in csv.DictReader(io.StringIO(text)):
            value = row.get(series)
            if value not in (None, "", "."):
                history[date.fromisoformat(row["observation_date"])] = float(value)
        return history
