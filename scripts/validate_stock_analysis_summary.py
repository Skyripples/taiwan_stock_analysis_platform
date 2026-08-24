"""Validate V3.12 rule transparency for fallback JSON and the live API."""

from __future__ import annotations

import argparse
import copy
import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from stock_analysis_summary import build_analysis_summary


SYMBOLS = ("2330", "2317", "6488", "2881", "1101", "0050")
RULE_PATH = PROJECT_ROOT / "config" / "stock_analysis_summary_rules.json"
STOCK_DIR = PROJECT_ROOT / "data" / "stocks"
REPORT = PROJECT_ROOT / "data" / "analysis" / "stock_analysis_summary_validation.json"
VALID = {"positive", "neutral", "warning", "unavailable"}


def expected(value: float, rule: dict[str, Any]) -> str:
    if "warning_min" in rule and value >= rule["warning_min"]: return "warning"
    if "warning_max" in rule and value <= rule["warning_max"]: return "warning"
    if "positive_min" in rule and value >= rule["positive_min"]: return "positive"
    if "positive_max" in rule and value <= rule["positive_max"]: return "positive"
    return "neutral"


def api_json(base: str, symbol: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base}/stocks/{symbol}", timeout=10) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--api-base", default="https://172-238-20-217.ip.linodeusercontent.com/api/v1")
    options = parser.parse_args()
    rules = json.loads(RULE_PATH.read_text(encoding="utf-8")); failures: list[dict[str, Any]] = []; results = {}
    rule_lookup = {(section, key): rule for section, section_rule in rules["sections"].items() for key, rule in (section_rule.get("metrics", {}) if isinstance(section_rule.get("metrics", {}), dict) else {}).items()}
    for symbol in SYMBOLS:
        fallback = json.loads((STOCK_DIR / f"{symbol}.json").read_text(encoding="utf-8"))["data"]
        summary = fallback.get("analysis_summary") or {}; sections = summary.get("overall_sections") or {}
        if set(sections) != set(rules["sections"]): failures.append({"symbol": symbol, "reason": "section set mismatch"})
        if len(summary.get("strengths", [])) > 5 or len(summary.get("risks", [])) > 5: failures.append({"symbol": symbol, "reason": "strength/risk limit exceeded"})
        for section, item in sections.items():
            if item.get("status") not in VALID: failures.append({"symbol": symbol, "section": section, "reason": "invalid status"})
            for evidence in item.get("evidence", []):
                if evidence.get("status") == "unavailable":
                    if evidence in summary.get("risks", []): failures.append({"symbol": symbol, "reason": "unavailable treated as risk"})
                    continue
                if evidence.get("value") is None or not evidence.get("date") or not evidence.get("threshold"):
                    failures.append({"symbol": symbol, "section": section, "key": evidence.get("key"), "reason": "evidence trace is incomplete"}); continue
                rule = rule_lookup.get((section, evidence.get("key")))
                if rule and expected(float(evidence["value"]), rule) != evidence["status"]:
                    failures.append({"symbol": symbol, "section": section, "key": evidence["key"], "reason": "rule recomputation mismatch"})
        if any(item.get("status") == "unavailable" for item in summary.get("risks", [])):
            failures.append({"symbol": symbol, "reason": "unavailable appears in risks"})
        try:
            api_summary = api_json(options.api_base, symbol).get("analysis_summary") or {}
            api_ok = set(api_summary.get("overall_sections", {})) == set(rules["sections"])
            for section, section_data in api_summary.get("overall_sections", {}).items():
                for evidence in section_data.get("evidence", []):
                    if evidence.get("status") != "unavailable" and (evidence.get("value") is None or not evidence.get("date") or not evidence.get("threshold")):
                        api_ok = False; failures.append({"symbol": symbol, "section": section, "key": evidence.get("key"), "reason": "API evidence trace is incomplete"})
        except Exception as exc:
            api_ok = False; failures.append({"symbol": symbol, "reason": f"API summary failed: {type(exc).__name__}"})
        results[symbol] = {"sections": {key: value.get("status") for key, value in sections.items()}, "strength_count": len(summary.get("strengths", [])), "risk_count": len(summary.get("risks", [])), "watch_count": len(summary.get("watch_items", [])), "api_summary": api_ok}

    etf = json.loads((STOCK_DIR / "0050.json").read_text(encoding="utf-8"))["data"]["analysis_summary"]
    for section in ("fundamentals", "valuation", "growth", "financial_safety", "peer_position"):
        if etf["overall_sections"][section]["status"] != "unavailable": failures.append({"symbol": "0050", "section": section, "reason": "ETF company rule was applied"})
    finance = json.loads((STOCK_DIR / "2881.json").read_text(encoding="utf-8"))["data"]["analysis_summary"]
    finance_keys = {item["key"] for item in finance["overall_sections"]["financial_safety"]["evidence"]}
    if finance_keys & {"debt_ratio", "current_ratio"}: failures.append({"symbol": "2881", "reason": "financial-industry exclusions failed"})

    missing = copy.deepcopy(json.loads((STOCK_DIR / "2330.json").read_text(encoding="utf-8"))["data"])
    missing.update({"fundamentals": {}, "historical_valuation": {}, "valuation_history_observations": [], "financial_trends": {}, "health_v2": {"categories": {}}, "chips": {}, "peer_analysis": {}})
    for item in (missing.get("valuation") or {}).values():
        if isinstance(item, dict): item["value"] = None
    degraded = build_analysis_summary(missing, rules)
    if any(section["status"] == "warning" for section in degraded["overall_sections"].values()): failures.append({"symbol": "missing_fixture", "reason": "missing data degraded to warning"})
    if not all(math.isfinite(float(item["value"])) for section in degraded["overall_sections"].values() for item in section["evidence"] if item.get("value") is not None): failures.append({"symbol": "missing_fixture", "reason": "non-finite evidence"})

    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": "passed" if not failures else "failed", "symbols": results, "missing_data_degrades_without_warning": not any(item.get("symbol") == "missing_fixture" for item in failures), "failure_count": len(failures), "failures": failures}
    REPORT.parent.mkdir(parents=True, exist_ok=True); temporary = REPORT.with_suffix(".json.tmp"); temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); temporary.replace(REPORT)
    print(json.dumps({"status": report["status"], "symbols": len(results), "failure_count": len(failures)}, ensure_ascii=False)); return 0 if not failures else 1


if __name__ == "__main__": raise SystemExit(main())
