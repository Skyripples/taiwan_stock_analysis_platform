"""Config-driven rule, module, and market-state scoring engine."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict

from .base_analysis import AnalysisResult, BaseAnalysis


class MarketSignalEngine(BaseAnalysis):
    analysis_name = "market_signal_engine"
    version = "2.0"
    LEGACY_SIGNALS = ("foreign_cash_flow", "foreign_futures_position", "night_futures", "tsm_adr", "sox_index")

    def __init__(self, market_data_dir: Path, factor_config_path: Path | None = None) -> None:
        self.market_data_dir = market_data_dir
        self.factor_config_path = factor_config_path or Path(__file__).resolve().parents[2] / "config" / "factor_config.json"
        self.config: Dict[str, Any] = {}

    def load(self) -> Dict[str, Any]:
        config = self._validate_config(self._load_config())
        loaded: Dict[str, Any] = {"factor_config": config}
        for source, filename in config["sources"].items():
            try:
                with (self.market_data_dir / filename).open("r", encoding="utf-8") as stream:
                    loaded[source] = json.load(stream)
            except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
                loaded[source] = {"_load_error": str(exc)}
        return loaded

    def analyze(self, source_data: Dict[str, Any]) -> AnalysisResult:
        self.config = self._validate_config(source_data.get("factor_config"))
        reference_date = self._reference_date(source_data)
        rules = {key: self._evaluate_rule(key, value, source_data, reference_date) for key, value in self.config["rules"].items()}
        modules = self._aggregate_modules(rules)
        enabled = [rule for rule in rules.values() if rule["enabled"] and self.config["modules"][rule["category"]]["enabled"]]
        available = [rule for rule in enabled if rule["available"]]
        stale = [rule["rule_id"] for rule in enabled if rule["stale"]]
        return {
            "rules": rules,
            "modules": modules,
            "market_score": self._aggregate_market(modules),
            "coverage": {"available_rules": len(available), "enabled_rules": len(enabled), "percentage": self._percent(len(available), len(enabled)), "excluded_rules": [rule["rule_id"] for rule in enabled if not rule["available"]]},
            "freshness": {"reference_date": reference_date.isoformat(), "status": "stale" if stale else ("complete" if len(available) == len(enabled) else "partial"), "stale_rules": stale},
        }

    def export(self, result: AnalysisResult, *, updated_at: str | None = None) -> AnalysisResult:
        payload = super().export(self._legacy_signals(result["rules"]), updated_at=updated_at)
        payload.update({key: result[key] for key in ("rules", "modules", "market_score", "coverage", "freshness")})
        payload.update({"score_type": "market_state_score", "score_disclaimer": "市場狀態分數，不是漲跌機率"})
        return payload

    def _evaluate_rule(self, rule_id: str, setting: Dict[str, Any], sources: Dict[str, Any], reference: date) -> Dict[str, Any]:
        result = {"rule_id": rule_id, "category": setting["module"], "display_name": setting["display_name"], "value": None, "score": None, "weight": setting["weight"], "status": "unavailable", "rationale": "資料缺漏，未納入計分", "enabled": setting["enabled"], "available": False, "stale": False, "source_trade_date": None}
        if not setting["enabled"]:
            result.update(status="disabled", rationale="規則已停用")
            return result
        record = self._record(sources.get(setting["source"]))
        if record is None:
            return result
        trade_date = self._parse_date(record.get("trade_date"))
        result["source_trade_date"] = trade_date.isoformat() if trade_date else None
        if trade_date is None:
            result["rationale"] = "來源日期缺漏，未納入計分"
            return result
        age = (reference - trade_date).days
        if age > setting["max_age_days"] or age < -setting["max_future_days"]:
            result.update(stale=True, rationale=f"資料日期不符合時效（相差 {age} 天），未納入計分")
            return result
        value = self._resolve_value(record, setting)
        if value is None:
            return result
        score = self._threshold_score(value * setting["direction"], setting["thresholds"])
        result.update(value=self._clean_number(value), score=score, status=self._rule_status(score), available=True, rationale=self._rationale(setting["display_name"], value, score, setting["thresholds"]))
        return result

    def _aggregate_modules(self, rules: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        output = {}
        for module_id, setting in self.config["modules"].items():
            configured = [rule for rule in rules.values() if rule["category"] == module_id and rule["enabled"]]
            if not setting["enabled"]:
                output[module_id] = {"module_id": module_id, "display_name": setting["display_name"], "score": None, "max_score": 2, "percentage": None, "status": "disabled", "weight": setting["weight"], "coverage": 0, "available_rules": 0, "enabled_rules": 0, "rule_ids": [rule["rule_id"] for rule in configured]}
                continue
            available = [rule for rule in configured if rule["available"]]
            denominator = sum(rule["weight"] for rule in available)
            score = None if denominator <= 0 else self._clean_number(sum(rule["score"] * rule["weight"] for rule in available) / denominator)
            percentage = None if score is None else self._score_percentage(score, 2)
            output[module_id] = {"module_id": module_id, "display_name": setting["display_name"], "score": score, "max_score": 2, "percentage": percentage, "status": "unavailable" if percentage is None else self._market_status(percentage), "weight": setting["weight"], "coverage": self._percent(len(available), len(configured)), "available_rules": len(available), "enabled_rules": len(configured), "rule_ids": [rule["rule_id"] for rule in configured]}
        return output

    def _aggregate_market(self, modules: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        available = [module for module in modules.values() if module["score"] is not None and module["weight"] > 0]
        if not available:
            raise ValueError("No market modules have current data")
        score = self._clean_number(sum(module["score"] * module["weight"] for module in available))
        maximum = self._clean_number(sum(2 * module["weight"] for module in available))
        percentage = self._score_percentage(score, maximum)
        return {"score": score, "max_score": maximum, "percentage": percentage, "status": self._market_status(percentage), "available_modules": len(available), "enabled_modules": len(modules)}

    def _legacy_signals(self, rules: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        output = {}
        for key in self.LEGACY_SIGNALS:
            rule = rules[key]
            output[key] = {"value": rule["value"], "status": rule["status"], "score": rule["score"], "enabled": rule["enabled"] and rule["available"], "weight": rule["weight"], "weighted_score": None if rule["score"] is None else self._clean_number(rule["score"] * rule["weight"])}
        return output

    def _resolve_value(self, record: Dict[str, Any], setting: Dict[str, Any]) -> float | None:
        if setting.get("formula") == "market_breadth":
            advancing, declining = self._number_at(record, ["advancing"]), self._number_at(record, ["declining"])
            return None if advancing is None or declining is None or advancing + declining == 0 else (advancing - declining) / (advancing + declining)
        return self._number_at(record, setting["value_path"])

    @staticmethod
    def _number_at(record: Dict[str, Any], path: list[str]) -> float | None:
        value: Any = record
        for key in path:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return float(value) if not isinstance(value, bool) and isinstance(value, (int, float)) else None

    @staticmethod
    def _record(payload: Any) -> Dict[str, Any] | None:
        data = payload.get("data") if isinstance(payload, dict) and not payload.get("_load_error") else None
        records = data.get("records") if isinstance(data, dict) else None
        return records[0] if isinstance(records, list) and records and isinstance(records[0], dict) else None

    def _reference_date(self, sources: Dict[str, Any]) -> date:
        record = self._record(sources.get("taiwan_market_overview"))
        result = self._parse_date(record.get("trade_date")) if record else None
        if result is None:
            raise ValueError("Taiwan market reference date is unavailable")
        return result

    def _load_config(self) -> Dict[str, Any]:
        try:
            with self.factor_config_path.open("r", encoding="utf-8") as stream:
                return json.load(stream)
        except FileNotFoundError as exc:
            raise ValueError(f"Factor configuration file not found: {self.factor_config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in factor configuration: {self.factor_config_path}") from exc

    def _validate_config(self, config: Any) -> Dict[str, Any]:
        if not isinstance(config, dict) or not all(isinstance(config.get(key), dict) and config[key] for key in ("sources", "modules", "rules")):
            raise ValueError("Factor configuration requires sources, modules, and rules")
        for module_id, module in config["modules"].items():
            self._validate_common(module, f"module {module_id}")
        for rule_id, rule in config["rules"].items():
            self._validate_common(rule, f"rule {rule_id}")
            if rule.get("module") not in config["modules"] or rule.get("source") not in config["sources"]:
                raise ValueError(f"Invalid module or source for rule {rule_id}")
            thresholds = rule.get("thresholds")
            values = [thresholds.get(key) for key in ("strong_bearish_max", "bearish_max", "bullish_min", "strong_bullish_min")] if isinstance(thresholds, dict) else []
            if len(values) != 4 or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values) or values != sorted(values) or values[1] >= values[2]:
                raise ValueError(f"Invalid thresholds for rule {rule_id}")
            if rule.get("direction") not in (-1, 1) or not isinstance(rule.get("max_age_days"), int) or not isinstance(rule.get("max_future_days"), int):
                raise ValueError(f"Invalid direction or freshness for rule {rule_id}")
            if not rule.get("formula") and not isinstance(rule.get("value_path"), list):
                raise ValueError(f"Rule {rule_id} requires value_path or formula")
        return config

    @staticmethod
    def _validate_common(setting: Dict[str, Any], label: str) -> None:
        weight = setting.get("weight") if isinstance(setting, dict) else None
        if not isinstance(setting, dict) or not isinstance(setting.get("enabled"), bool) or not setting.get("display_name") or isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
            raise ValueError(f"Invalid {label}")

    @staticmethod
    def _threshold_score(value: float, thresholds: Dict[str, float]) -> int:
        if value <= thresholds["strong_bearish_max"]: return -2
        if value < thresholds["bearish_max"]: return -1
        if value < thresholds["bullish_min"]: return 0
        if value < thresholds["strong_bullish_min"]: return 1
        return 2

    @staticmethod
    def _rule_status(score: int) -> str:
        return {-2: "strong_bearish", -1: "bearish", 0: "neutral", 1: "bullish", 2: "strong_bullish"}[score]

    @staticmethod
    def _rationale(name: str, value: float, score: int, thresholds: Dict[str, float]) -> str:
        return f"{name}={value:.4g}，依設定門檻評為 {MarketSignalEngine._rule_status(score)}（{score:+d}）；門檻={thresholds}"

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        try: return date.fromisoformat(str(value))
        except (TypeError, ValueError): return None

    @staticmethod
    def _clean_number(value: float) -> int | float:
        rounded = round(value, 10)
        return int(rounded) if float(rounded).is_integer() else rounded

    @staticmethod
    def _percent(numerator: int, denominator: int) -> int | float:
        return 0 if denominator <= 0 else MarketSignalEngine._clean_number(numerator / denominator * 100)

    @staticmethod
    def _score_percentage(score: float, maximum: float) -> int | float:
        return 50 if maximum <= 0 else MarketSignalEngine._clean_number(max(0, min(100, (score + maximum) / (2 * maximum) * 100)))

    @staticmethod
    def _market_status(percentage: float) -> str:
        if percentage <= 20: return "Strong Bearish"
        if percentage < 40: return "Bearish"
        if percentage < 60: return "Neutral"
        if percentage < 80: return "Bullish"
        return "Strong Bullish"
