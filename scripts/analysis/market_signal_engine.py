"""Generate basic market signals from normalized provider output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .base_analysis import AnalysisResult, BaseAnalysis


class MarketSignalEngine(BaseAnalysis):
    """Analyze foreign cash flow and foreign futures positioning."""

    analysis_name = "market_signal_engine"

    def __init__(self, market_data_dir: Path) -> None:
        self.market_data_dir = market_data_dir
        self.source_files = {
            "institutional_investors": market_data_dir / "institutional_investors.json",
            "foreign_futures_position": market_data_dir / "foreign_futures_position.json",
        }

    def load(self) -> Dict[str, Any]:
        """Load all required market data JSON files."""

        loaded_data: Dict[str, Any] = {}
        for source_name, source_path in self.source_files.items():
            try:
                with source_path.open("r", encoding="utf-8") as source_file:
                    loaded_data[source_name] = json.load(source_file)
            except FileNotFoundError as exc:
                raise ValueError(f"Required market data file not found: {source_path}") from exc
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in market data file: {source_path}") from exc
        return loaded_data

    def analyze(self, source_data: Dict[str, Any]) -> AnalysisResult:
        """Create directional signals without forecasting future prices."""

        cash_record = self._latest_record(
            source_data.get("institutional_investors"),
            "institutional_investors",
        )
        futures_record = self._latest_record(
            source_data.get("foreign_futures_position"),
            "foreign_futures_position",
        )

        foreign_cash_flow = self._require_integer(
            cash_record,
            ("foreign_and_mainland_investors", "net"),
            "institutional_investors",
        )
        foreign_futures_position = self._require_integer(
            futures_record,
            ("net_position", "open_interest"),
            "foreign_futures_position",
        )

        return {
            "foreign_cash_flow": {
                "value": foreign_cash_flow,
                "status": self._direction(foreign_cash_flow),
            },
            "foreign_futures_position": {
                "value": foreign_futures_position,
                "status": self._direction(foreign_futures_position),
            },
        }

    @staticmethod
    def _latest_record(payload: Any, source_name: str) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid payload for {source_name}")

        data = payload.get("data")
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list) or not records or not isinstance(records[0], dict):
            raise ValueError(f"No valid records found in {source_name}")
        return records[0]

    @staticmethod
    def _require_integer(
        record: Dict[str, Any],
        field_path: tuple[str, ...],
        source_name: str,
    ) -> int:
        value: Any = record
        for field in field_path:
            if not isinstance(value, dict) or field not in value:
                dotted_path = ".".join(field_path)
                raise ValueError(f"Missing {dotted_path} in {source_name}")
            value = value[field]

        if isinstance(value, bool) or not isinstance(value, int):
            dotted_path = ".".join(field_path)
            raise ValueError(f"{dotted_path} in {source_name} must be an integer")
        return value

    @staticmethod
    def _direction(value: int) -> str:
        if value > 0:
            return "bullish"
        if value < 0:
            return "bearish"
        return "neutral"
