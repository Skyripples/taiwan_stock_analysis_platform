"""Transparent rule-based stock summary shared by JSON builds and the API."""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any


STATUSES = {"positive", "neutral", "warning", "unavailable"}


def number(value: Any) -> float | int | None:
    if isinstance(value, dict):
        value = value.get("value")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def metric(data: dict[str, Any], key: str) -> tuple[Any, str | None]:
    fundamentals = data.get("fundamentals") or {}
    trends = (data.get("financial_trends") or {}).get("summary") or {}
    item = fundamentals.get(key)
    if key in {"gross_margin_trend", "operating_margin_trend"}:
        return number(trends.get(key)), (data.get("financial_trends") or {}).get("latest_period")
    if isinstance(item, dict):
        return number(item), item.get("data_date")
    direct = number(item)
    if direct is not None:
        return direct, fundamentals.get("report_date")
    health_key = "eps_yoy" if key == "eps_growth" else key
    for items in ((data.get("health_v2") or {}).get("categories") or {}).values():
        for health_item in items or []:
            if health_item.get("key") == health_key:
                return number(health_item.get("value")), health_item.get("data_date")
    return None, fundamentals.get("report_date")


def threshold_text(rule: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, operator in (("positive_min", "positive ≥"), ("positive_max", "positive ≤"), ("warning_min", "warning ≥"), ("warning_max", "warning ≤")):
        if key in rule:
            parts.append(f"{operator} {rule[key]}")
    return "；".join(parts)


def classify(value: Any, rule: dict[str, Any]) -> str:
    if number(value) is None:
        return "unavailable"
    value = number(value)
    if "warning_min" in rule and value >= rule["warning_min"]:
        return "warning"
    if "warning_max" in rule and value <= rule["warning_max"]:
        return "warning"
    if "positive_min" in rule and value >= rule["positive_min"]:
        return "positive"
    if "positive_max" in rule and value <= rule["positive_max"]:
        return "positive"
    return "neutral"


def historical_percentiles(data: dict[str, Any]) -> dict[str, tuple[Any, str | None]]:
    output: dict[str, tuple[Any, str | None]] = {}
    historical = data.get("historical_valuation") or {}
    valuation = data.get("valuation") or {}
    mapping = {"pe_percentile": "pe", "pb_percentile": "pb", "yield_percentile": "dividend_yield"}
    observations = data.get("valuation_history_observations") or []
    for output_key, field in mapping.items():
        value = (((historical.get(field) or {}).get("5y") or {}).get("current_percentile"))
        date = ((valuation.get(field) or {}).get("data_date") if isinstance(valuation.get(field), dict) else valuation.get("valuation_date"))
        if value is None and observations:
            current = number(valuation.get(field))
            values = [number(row.get(field)) for row in observations]
            values = [item for item in values if item is not None]
            if current is not None and values:
                value = round(sum(item <= current for item in values) / len(values) * 100, 4)
                date = observations[-1].get("trade_date")
        output[output_key] = (value, date)
    return output


def chips_metrics(data: dict[str, Any]) -> dict[str, tuple[Any, str | None]]:
    chips = data.get("chips") or {}
    analysis = chips.get("analysis") or {}
    summary = chips.get("summary") or {}
    date = chips.get("trade_date") or summary.get("trade_date")
    return {
        "foreign_5d": ((analysis.get("foreign_sum") or {}).get("5d", summary.get("foreign_5d")), date),
        "foreign_20d": ((analysis.get("foreign_sum") or {}).get("20d", summary.get("foreign_20d")), date),
        "investment_trust_5d": ((analysis.get("investment_trust_sum") or {}).get("5d", summary.get("investment_trust_5d")), date),
        "institutional_20d": ((analysis.get("institutional_sum") or {}).get("20d", summary.get("institutional_20d")), date),
    }


def peer_metrics(data: dict[str, Any], snapshot: list[dict[str, Any]] | None, rules: dict[str, Any]) -> list[dict[str, Any]]:
    profile = data.get("profile") or {}
    result: list[dict[str, Any]] = []
    stored = (data.get("peer_analysis") or {}).get("metrics") or []
    allowed = set(rules["sections"]["peer_position"]["metrics"])
    labels = {"roe": "同業 ROE", "eps": "同業 EPS", "revenue_yoy": "同業營收 YoY", "operating_margin": "同業營業利益率", "net_margin": "同業淨利率", "pe": "同業 PE", "pb": "同業 PB"}
    for item in stored:
        if item.get("metric_key") not in allowed or number(item.get("percentile")) is None:
            continue
        percentile = number(item["percentile"])
        status = "positive" if percentile >= rules["sections"]["peer_position"]["positive_percentile_min"] else "warning" if percentile < rules["sections"]["peer_position"]["warning_percentile_max"] else "neutral"
        valuation = data.get("valuation") or {}; fundamentals = data.get("fundamentals") or {}
        fallback_date = valuation.get("valuation_date") or fundamentals.get("report_date") or data.get("quote", {}).get("trade_date")
        result.append({"key": item["metric_key"], "label": labels.get(item["metric_key"], f"同業 {item['metric_key']}"), "value": item.get("company_value"), "unit": None, "threshold": "依同產業有效樣本百分位；≥60 positive，<40 warning", "date": item.get("comparison_period") or fallback_date, "status": status, "percentile": percentile, "sample_size": item.get("sample_size")})
    if result or not snapshot:
        return result
    industry = profile.get("industry")
    peers = [row for row in snapshot if row.get("industry") == industry and row.get("instrument_type") == "company"]
    current = next((row for row in peers if str(row.get("symbol")) == str(profile.get("symbol"))), None)
    if not current or len(peers) < rules.get("minimum_peer_samples", 5):
        return []
    directions = {"pe": "lower", "pb": "lower"}
    for key in allowed:
        current_value = number(current.get(key))
        values = [number(row.get(key)) for row in peers]
        values = [value for value in values if value is not None and not (key == "pe" and value <= 0)]
        if current_value is None or len(values) < rules.get("minimum_peer_samples", 5):
            continue
        percentile = (sum(value >= current_value for value in values) if directions.get(key) == "lower" else sum(value <= current_value for value in values)) / len(values) * 100
        status = "positive" if percentile >= 60 else "warning" if percentile < 40 else "neutral"
        result.append({"key": key, "label": labels.get(key, key), "value": current_value, "unit": None, "threshold": "依同產業有效樣本百分位；≥60 positive，<40 warning", "date": current.get("financial_date") or current.get("valuation_date"), "status": status, "percentile": round(percentile, 4), "industry_median": median(values), "sample_size": len(values)})
    return result


def aggregate(label: str, evidence: list[dict[str, Any]], unavailable_summary: str | None = None) -> dict[str, Any]:
    available = [item for item in evidence if item["status"] != "unavailable"]
    if not available:
        return {"status": "unavailable", "summary": unavailable_summary or f"{label}資料不足，暫無法判斷。", "evidence": evidence}
    positives = sum(item["status"] == "positive" for item in available)
    warnings = sum(item["status"] == "warning" for item in available)
    status = "positive" if positives > warnings else "warning" if warnings > positives else "neutral"
    summary = f"{label}可用指標 {len(available)} 項：正向 {positives}、中性 {len(available) - positives - warnings}、警示 {warnings}。"
    return {"status": status, "summary": summary, "evidence": evidence}


def build_analysis_summary(data: dict[str, Any], rules: dict[str, Any], peer_snapshot: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    profile = data.get("profile") or {}
    is_company = profile.get("instrument_type") == "company"
    exclusions = rules.get("industry_exclusions", {}).get(profile.get("industry"), {})
    sections: dict[str, dict[str, Any]] = {}
    all_evidence: list[tuple[str, dict[str, Any], int]] = []
    historical = historical_percentiles(data)
    chips = chips_metrics(data)
    for section_key, section_rule in rules["sections"].items():
        label = section_rule["label"]
        if section_key == "peer_position":
            evidence = peer_metrics(data, peer_snapshot, rules) if is_company else []
            sections[section_key] = aggregate(label, evidence, "ETF 不適用公司同業財務排名。" if not is_company else None)
            all_evidence.extend((section_key, item, 60) for item in evidence)
            continue
        if not is_company and section_key in {"fundamentals", "valuation", "growth", "financial_safety"}:
            sections[section_key] = {"status": "unavailable", "summary": "ETF 不適用公司基本面、財務趨勢或歷史估值規則。", "evidence": []}
            continue
        evidence = []
        for key, rule in section_rule.get("metrics", {}).items():
            if key in exclusions.get(section_key, []):
                continue
            if section_key == "valuation":
                value, date = historical[key]
            elif section_key == "chips":
                value, date = chips[key]
            else:
                value, date = metric(data, key)
            item = {"key": key, "label": rule["label"], "value": value, "unit": rule.get("unit"), "threshold": threshold_text(rule), "date": date, "status": classify(value, rule)}
            evidence.append(item)
            all_evidence.append((section_key, item, int(rule.get("priority", 0))))
        sections[section_key] = aggregate(label, evidence)
    strengths = [{"section": section, **item} for section, item, _ in sorted((entry for entry in all_evidence if entry[1]["status"] == "positive"), key=lambda entry: entry[2], reverse=True)[:rules.get("maximum_strengths", 5)]]
    risks = [{"section": section, **item} for section, item, _ in sorted((entry for entry in all_evidence if entry[1]["status"] == "warning"), key=lambda entry: entry[2], reverse=True)[:rules.get("maximum_risks", 5)]]
    watch_items = []
    seen_watch: set[str] = set()
    for key, value in sections.items():
        if value["status"] == "unavailable" and value["summary"] not in seen_watch:
            watch_items.append({"section": key, "summary": value["summary"]})
            seen_watch.add(value["summary"])
    for section, item, _ in all_evidence:
        if item["status"] == "unavailable" and len(watch_items) < 10:
            text = f"{item['label']}資料不足，等待更新。"
            if text not in seen_watch:
                watch_items.append({"section": section, "summary": text, "date": item.get("date")})
                seen_watch.add(text)
    dates = [item.get("date") for _, item, _ in all_evidence if item.get("date")]
    return {"version": rules["version"], "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "data_date": max(dates) if dates else data.get("quote", {}).get("trade_date"), "instrument_type": profile.get("instrument_type"), "overall_sections": sections, "strengths": strengths, "risks": risks, "watch_items": watch_items}
