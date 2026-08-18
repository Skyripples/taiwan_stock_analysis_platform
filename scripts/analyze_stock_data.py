"""Apply valuation, health, chips-trend, and industry analyses to stock caches."""

from __future__ import annotations

import json
import math
import os
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from config import PROJECT_ROOT


STOCK_DIR = PROJECT_ROOT / "data" / "stocks"
HISTORY_DIR = STOCK_DIR / "history"
RULE_PATH = PROJECT_ROOT / "config" / "stock_health_rules.json"
SNAPSHOT_PATH = STOCK_DIR / "industry_snapshot.json"


def atomic(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def percentile(values: list[float], probability: float) -> float | None:
    clean = sorted(value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool))
    if not clean: return None
    position = (len(clean) - 1) * probability; lower = math.floor(position); upper = math.ceil(position)
    result = clean[lower] if lower == upper else clean[lower] + (clean[upper] - clean[lower]) * (position - lower)
    return round(result, 6)


def current_percentile(values: list[float], current: float | None) -> float | None:
    clean = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return round(sum(value <= current for value in clean) / len(clean) * 100, 4) if clean and isinstance(current, (int, float)) else None


def distribution(observations: list[dict[str, Any]], field: str, current: float | None, years: int) -> dict[str, Any]:
    if not observations: return {"sample_count": 0}
    cutoff = date(date.fromisoformat(observations[-1]["trade_date"]).year - years, date.fromisoformat(observations[-1]["trade_date"]).month, 1)
    values = [row[field] for row in observations if date.fromisoformat(row["trade_date"]) >= cutoff and isinstance(row.get(field), (int, float))]
    return {"sample_count": len(values), "median": percentile(values, .5), "p25": percentile(values, .25), "p50": percentile(values, .5), "p75": percentile(values, .75), "current_percentile": current_percentile(values, current), "low": min(values) if values else None, "high": max(values) if values else None}


def status(value: Any, rule: dict[str, Any]) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool): return "unavailable"
    if "pass_min" in rule and value >= rule["pass_min"]: return "pass"
    if "pass_max" in rule and value <= rule["pass_max"]: return "pass"
    if "warning_max" in rule and value < rule["warning_max"]: return "warning"
    if "warning_min" in rule and value >= rule["warning_min"]: return "warning"
    return "neutral"


def rule_text(rule: dict[str, Any]) -> str:
    parts=[]
    if "pass_min" in rule: parts.append(f"pass ≥ {rule['pass_min']}")
    if "pass_max" in rule: parts.append(f"pass ≤ {rule['pass_max']}")
    if "warning_max" in rule: parts.append(f"warning < {rule['warning_max']}")
    if "warning_min" in rule: parts.append(f"warning ≥ {rule['warning_min']}")
    return "；".join(parts)


def chips_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def sums(field: str) -> dict[str, int | None]: return {f"{size}d": sum(row[field] for row in rows[-size:]) if len(rows) >= size else None for size in (5,20,60)}
    def streak(field: str) -> dict[str, Any]:
        sign=1 if rows[-1][field]>0 else -1 if rows[-1][field]<0 else 0; days=0
        for row in reversed(rows):
            if (1 if row[field]>0 else -1 if row[field]<0 else 0)!=sign: break
            days+=1
        return {"direction":"buy" if sign>0 else "sell" if sign<0 else "neutral","days":days}
    return {"foreign_sum":sums("foreign_net"),"investment_trust_sum":sums("investment_trust_net"),"institutional_sum":sums("institutional_total"),"foreign_streak":streak("foreign_net"),"investment_trust_streak":streak("investment_trust_net"),"margin_change":{"5d":rows[-1]["margin_balance"]-rows[-6]["margin_balance"] if len(rows)>=6 else None,"20d":rows[-1]["margin_balance"]-rows[-21]["margin_balance"] if len(rows)>=21 else None}}


def health_values(data: dict[str, Any]) -> dict[str, Any]:
    f=data["fundamentals"]; history=data.get("fundamental_history",{})
    return {"revenue_yoy":f.get("revenue_yoy",{}).get("value"),"eps_growth":f.get("eps_growth",{}).get("value"),"roe":f.get("roe",{}).get("value"),"gross_margin":f.get("gross_margin",{}).get("value"),"operating_margin":f.get("operating_margin",{}).get("value"),"debt_ratio":f.get("debt_ratio",{}).get("value"),"current_ratio":f.get("current_ratio",{}).get("value"),"eps":f.get("eps",{}).get("value"),"operating_cash_flow":f.get("operating_cash_flow",{}).get("value"),"gross_margin_trend":history.get("gross_margin_trend"),"operating_margin_trend":history.get("operating_margin_trend"),"consecutive_losses":history.get("consecutive_losses"),"pe_percentile":data.get("historical_valuation",{}).get("pe",{}).get("5y",{}).get("current_percentile"),"pb_percentile":data.get("historical_valuation",{}).get("pb",{}).get("5y",{}).get("current_percentile"),"yield_percentile":data.get("historical_valuation",{}).get("dividend_yield",{}).get("5y",{}).get("current_percentile")}


def make_health(data: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if data["profile"]["instrument_type"]!="company": return {"applicable":False,"reason":"ETF／非一般公司不適用公司健檢","rules_version":config["version"],"categories":{}}
    values=health_values(data); dates={key:value.get("data_date") for key,value in data["fundamentals"].items() if isinstance(value,dict)}
    labels={"revenue_yoy":"月營收 YoY","eps_growth":"EPS 成長","roe":"ROE","gross_margin_trend":"毛利率趨勢","operating_margin_trend":"營業利益率趨勢","consecutive_losses":"連續虧損期數","debt_ratio":"負債比","current_ratio":"流動比率","operating_cash_flow":"營業現金流","pe_percentile":"PE 歷史百分位","pb_percentile":"PB 歷史百分位","yield_percentile":"殖利率歷史百分位","eps":"EPS","gross_margin":"毛利率"}
    output={}
    for category,rules in config["categories"].items(): output[category]=[{"key":key,"label":labels[key],"value":values.get(key),"status":status(values.get(key),rule),"threshold":rule_text(rule),"data_date":dates.get(key) or data["fundamentals"].get("report_date") or data["quote"].get("trade_date")} for key,rule in rules.items()]
    return {"applicable":True,"rules_version":config["version"],"categories":output}


def industry_comparison(data: dict[str, Any], snapshot: list[dict[str, Any]], minimum: int) -> dict[str, Any]:
    if data["profile"]["instrument_type"]!="company": return {"applicable":False,"reason":"ETF 不適用同產業公司比較"}
    industry=data["profile"]["industry"]; peers=[row for row in snapshot if row.get("industry")==industry and row.get("instrument_type")=="company"]
    metrics={"pe":data["valuation"]["pe"]["value"],"pb":data["valuation"]["pb"]["value"],"dividend_yield":data["valuation"]["dividend_yield"]["value"],"roe":data["fundamentals"]["roe"]["value"],"eps":data["fundamentals"]["eps"]["value"],"revenue_yoy":data["fundamentals"]["revenue_yoy"]["value"]}
    result={}
    for key,current in metrics.items():
        values=[row[key] for row in peers if isinstance(row.get(key),(int,float))]
        result[key]={"current":current,"industry_median":round(median(values),6) if len(values)>=minimum else None,"percentile":current_percentile(values,current) if len(values)>=minimum else None,"sample_count":len(values),"status":"available" if len(values)>=minimum else "insufficient"}
    return {"applicable":True,"industry":industry,"minimum_samples":minimum,"metrics":result}


def main() -> int:
    config=json.loads(RULE_PATH.read_text(encoding="utf-8")); snapshot=json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["stocks"]
    count=0
    for path in STOCK_DIR.glob("*.json"):
        if path.name in {"index.json","industry_snapshot.json"}: continue
        payload=json.loads(path.read_text(encoding="utf-8")); data=payload["data"]
        if data["profile"]["instrument_type"]=="company":
            history_path=HISTORY_DIR/f"{data['profile']['symbol']}_valuation.json"
            observations=json.loads(history_path.read_text(encoding="utf-8"))["data"]["observations"] if history_path.exists() else []
            data["historical_valuation"]={field:{"current":data["valuation"][field]["value"],"3y":distribution(observations,field,data["valuation"][field]["value"],3),"5y":distribution(observations,field,data["valuation"][field]["value"],5),"source_frequency":"monthly_last_available_trading_day"} for field in ("pe","pb","dividend_yield")}
        else: data["historical_valuation"]={"applicable":False,"reason":"ETF 不強制套用公司歷史估值"}
        data["chips"]["analysis"]=chips_analysis(data["chips"]["history"])
        data["health_v2"]=make_health(data,config)
        data["industry_comparison"]=industry_comparison(data,snapshot,config["minimum_industry_samples"])
        atomic(path,payload); count+=1
    print(f"Stock analysis updated: {count} caches")
    return 0


if __name__=="__main__": raise SystemExit(main())
