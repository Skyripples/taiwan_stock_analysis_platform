"""Generate basic market signals from normalized provider output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .base_analysis import AnalysisResult, BaseAnalysis


class MarketSignalEngine(BaseAnalysis):
    """Analyze foreign cash flow and foreign futures positioning."""

    analysis_name = "market_signal_engine"
    SCORE_BY_STATUS = {
        "bullish": 1,
        "neutral": 0,
        "bearish": -1,
    }

    FACTOR_NAMES = (
        "foreign_cash_flow",
        "foreign_futures_position",
        "night_futures",
        "tsm_adr",
        "sox_index",
    )

    def __init__(self, market_data_dir: Path, factor_config_path: Path | None = None) -> None:
        self.market_data_dir = market_data_dir
        self.factor_config_path = factor_config_path or Path(__file__).resolve().parents[2] / "config" / "factor_config.json"
        self.factor_config: Dict[str, Dict[str, Any]] = {}
        self.source_files = {
            "institutional_investors": market_data_dir / "institutional_investors.json",
            "foreign_futures_position": market_data_dir / "foreign_futures_position.json",
            "night_futures": market_data_dir / "night_futures.json",
            "tsm_adr": market_data_dir / "tsm_adr.json",
            "sox_index": market_data_dir / "sox_index.json",
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
        loaded_data["factor_config"] = self._load_factor_config()
        return loaded_data

    def analyze(self, source_data: Dict[str, Any]) -> AnalysisResult:
        """Create directional signals without forecasting future prices."""

        self.factor_config = self._validate_factor_config(source_data.get("factor_config"))

        cash_record = self._latest_record(
            source_data.get("institutional_investors"),
            "institutional_investors",
        )
        futures_record = self._latest_record(
            source_data.get("foreign_futures_position"),
            "foreign_futures_position",
        )
        night_futures_record = self._latest_record(
            source_data.get("night_futures"),
            "night_futures",
        )
        tsm_adr_record = self._latest_record(
            source_data.get("tsm_adr"),
            "tsm_adr",
        )
        sox_index_record = self._latest_record(
            source_data.get("sox_index"),
            "sox_index",
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
        night_futures_change = self._require_number(
            night_futures_record,
            ("change",),
            "night_futures",
        )
        tsm_adr_change = self._require_number(
            tsm_adr_record,
            ("change",),
            "tsm_adr",
        )
        sox_index_change_percent = self._require_number(
            sox_index_record,
            ("change_percent",),
            "sox_index",
        )

        signals = {
            "foreign_cash_flow": self._build_signal(foreign_cash_flow),
            "foreign_futures_position": self._build_signal(foreign_futures_position),
            "night_futures": self._build_signal(night_futures_change),
            "tsm_adr": self._build_signal(tsm_adr_change),
            "sox_index": self._build_signal(sox_index_change_percent),
        }
        for factor_name, signal in signals.items():
            setting = self.factor_config[factor_name]
            signal["enabled"] = setting["enabled"]
            signal["weight"] = setting["weight"]
            signal["weighted_score"] = self._clean_number(signal["score"] * setting["weight"])
        return signals

    def export(
        self,
        analysis_result: AnalysisResult,
        *,
        updated_at: str | None = None,
    ) -> AnalysisResult:
        """Export existing signals together with their aggregate Market Score."""

        payload = super().export(analysis_result, updated_at=updated_at)
        payload["market_score"] = self._calculate_market_score(analysis_result)
        return payload

    def _build_signal(self, value: int | float) -> Dict[str, Any]:
        status = self._direction(value)
        return {
            "value": value,
            "status": status,
            "score": self.SCORE_BY_STATUS[status],
        }

    def _calculate_market_score(self, signals: AnalysisResult) -> Dict[str, Any]:
        enabled_signals = [signal for signal in signals.values() if signal.get("enabled") is True]
        if not enabled_signals:
            raise ValueError("At least one market factor must be enabled")
        scores = [self._weighted_signal_score(signal) for signal in enabled_signals]
        maximum_per_signal = max(abs(score) for score in self.SCORE_BY_STATUS.values())
        max_score = self._clean_number(sum(signal["weight"] * maximum_per_signal for signal in enabled_signals))
        if max_score <= 0:
            raise ValueError("Enabled market factor weights must have a positive total")
        score = self._clean_number(sum(scores))
        percentage = self._score_percentage(score, max_score)

        return {
            "score": score,
            "max_score": max_score,
            "percentage": percentage,
            "status": self._market_status(percentage),
        }

    def _signal_score(self, signal: Any) -> int:
        if not isinstance(signal, dict):
            raise ValueError("Each market signal must be an object")

        status = signal.get("status")
        score = signal.get("score")
        if status not in self.SCORE_BY_STATUS or score != self.SCORE_BY_STATUS[status]:
            raise ValueError(f"Invalid score for market signal status: {status}")
        return score

    def _weighted_signal_score(self, signal: Any) -> int | float:
        score = self._signal_score(signal)
        weight = signal.get("weight")
        weighted_score = signal.get("weighted_score")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
            raise ValueError("Market signal weight must be a non-negative number")
        expected = self._clean_number(score * weight)
        if weighted_score != expected:
            raise ValueError("Market signal weighted_score does not match score × weight")
        return weighted_score

    @staticmethod
    def _score_percentage(score: int | float, max_score: int | float) -> int | float:
        if max_score <= 0:
            return 50

        percentage = round(((score + max_score) / (2 * max_score)) * 100, 2)
        return int(percentage) if percentage.is_integer() else percentage

    def _load_factor_config(self) -> Dict[str, Any]:
        try:
            with self.factor_config_path.open("r", encoding="utf-8") as config_file:
                return json.load(config_file)
        except FileNotFoundError as exc:
            raise ValueError(f"Factor configuration file not found: {self.factor_config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in factor configuration: {self.factor_config_path}") from exc

    def _validate_factor_config(self, config: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(config, dict):
            raise ValueError("Factor configuration must be an object")
        missing = sorted(set(self.FACTOR_NAMES).difference(config))
        if missing:
            raise ValueError(f"Factor configuration is missing: {', '.join(missing)}")

        validated: Dict[str, Dict[str, Any]] = {}
        enabled_count = 0
        for factor_name in self.FACTOR_NAMES:
            setting = config[factor_name]
            if not isinstance(setting, dict):
                raise ValueError(f"Factor configuration for {factor_name} must be an object")
            enabled = setting.get("enabled")
            weight = setting.get("weight")
            display_name = setting.get("display_name")
            description = setting.get("description")
            if not isinstance(enabled, bool):
                raise ValueError(f"Factor enabled must be boolean: {factor_name}")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
                raise ValueError(f"Factor weight must be a non-negative number: {factor_name}")
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError(f"Factor display_name is required: {factor_name}")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"Factor description is required: {factor_name}")
            validated[factor_name] = {
                "enabled": enabled,
                "weight": self._clean_number(weight),
                "display_name": display_name.strip(),
                "description": description.strip(),
            }
            enabled_count += int(enabled)
        if enabled_count == 0:
            raise ValueError("At least one market factor must be enabled")
        return validated

    @staticmethod
    def _clean_number(value: int | float) -> int | float:
        rounded = round(value, 10)
        return int(rounded) if float(rounded).is_integer() else rounded

    @staticmethod
    def _market_status(percentage: int | float) -> str:
        if percentage >= 70:
            return "Strong Bullish"
        if percentage > 50:
            return "Bullish"
        if percentage == 50:
            return "Neutral"
        if percentage <= 30:
            return "Strong Bearish"
        return "Bearish"

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
    def _require_number(
        record: Dict[str, Any],
        field_path: tuple[str, ...],
        source_name: str,
    ) -> int | float:
        value: Any = record
        for field in field_path:
            if not isinstance(value, dict) or field not in value:
                dotted_path = ".".join(field_path)
                raise ValueError(f"Missing {dotted_path} in {source_name}")
            value = value[field]

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            dotted_path = ".".join(field_path)
            raise ValueError(f"{dotted_path} in {source_name} must be a number")
        return value

    @staticmethod
    def _direction(value: int | float) -> str:
        if value > 0:
            return "bullish"
        if value < 0:
            return "bearish"
        return "neutral"
