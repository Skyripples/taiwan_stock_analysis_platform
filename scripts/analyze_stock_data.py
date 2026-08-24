"""Apply valuation, health, chips-trend, and industry analyses to stock caches."""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from config import PROJECT_ROOT
from stock_analysis_summary import build_analysis_summary


STOCK_DIR = PROJECT_ROOT / "data" / "stocks"
HISTORY_DIR = STOCK_DIR / "history"
FINANCIAL_DIR = STOCK_DIR / "financials"
RULE_PATH = PROJECT_ROOT / "config" / "stock_health_rules.json"
SNAPSHOT_PATH = STOCK_DIR / "industry_snapshot.json"
SUMMARY_RULE_PATH = PROJECT_ROOT / "config" / "stock_analysis_summary_rules.json"


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


def change_percent(current: Any, previous: Any) -> float | None:
    if not isinstance(current,(int,float)) or not isinstance(previous,(int,float)) or previous==0: return None
    return round((current-previous)/abs(previous)*100,6)


def streak(rows: list[dict[str,Any]], predicate: Any) -> int | None:
    if not rows: return None
    count=0
    for row in reversed(rows):
        if not predicate(row): break
        count+=1
    return count


def financial_trends(rows: list[dict[str,Any]]) -> dict[str,Any]:
    if len(rows)<8: return {"applicable":False,"reason":"多期財報不足 8 季"}
    enriched=[]
    for index,row in enumerate(rows):
        item=dict(row); previous=rows[index-1] if index else {}; year_ago=rows[index-4] if index>=4 else {}
        for field in ("eps","revenue","gross_margin","operating_margin","net_margin"):
            item[f"{field}_qoq"]=change_percent(row.get(field),previous.get(field))
            item[f"{field}_yoy"]=change_percent(row.get(field),year_ago.get(field))
        window=rows[max(0,index-3):index+1]
        item["ttm_eps"]=round(sum(value["eps"] for value in window),6) if len(window)==4 and all(isinstance(value.get("eps"),(int,float)) for value in window) else None
        item["ttm_operating_cash_flow"]=sum(value["operating_cash_flow"] for value in window) if len(window)==4 and all(isinstance(value.get("operating_cash_flow"),(int,float)) for value in window) else None
        item["ttm_free_cash_flow"]=sum(value["free_cash_flow"] for value in window) if len(window)==4 and all(isinstance(value.get("free_cash_flow"),(int,float)) for value in window) else None
        enriched.append(item)
    latest=enriched[-1]; recent=enriched[-4:]
    def delta(field:str)->float|None:
        values=[row.get(field) for row in recent]
        return round(values[-1]-values[0],6) if len(values)==4 and all(isinstance(value,(int,float)) for value in values) else None
    def consecutive_yoy(sign:int)->int|None:
        available=[row.get("eps_yoy") for row in enriched if isinstance(row.get("eps_yoy"),(int,float))]
        if not available:return None
        count=0
        for value in reversed(available):
            if (value>0 if sign>0 else value<0):count+=1
            else:break
        return count
    def direction(field:str,tolerance:float=0.0)->str|None:
        values=[row.get(field) for row in recent]
        if len(values)!=4 or not all(isinstance(value,(int,float)) for value in values):return None
        changes=[values[index]-values[index-1] for index in range(1,len(values))]
        if all(value>tolerance for value in changes):return "improving"
        if all(value<-tolerance for value in changes):return "deteriorating"
        if max(values)-min(values)<=tolerance:return "stable"
        return "mixed"
    margin_ranges=[]
    for field in ("gross_margin","operating_margin","net_margin"):
        values=[row.get(field) for row in recent]
        if len(values)!=4 or not all(isinstance(value,(int,float)) for value in values): margin_ranges=[]; break
        margin_ranges.append(max(values)-min(values))
    stability=round(max(margin_ranges),6) if margin_ranges else None
    return {"applicable":True,"period_count":len(rows),"latest_period":latest["period_end"],"records":enriched,
        "summary":{"eps_qoq":latest.get("eps_qoq"),"eps_yoy":latest.get("eps_yoy"),"ttm_eps":latest.get("ttm_eps"),"eps_growth_streak":consecutive_yoy(1),"eps_decline_streak":consecutive_yoy(-1),
        "quarter_revenue_yoy":latest.get("revenue_yoy"),"revenue_direction_4q":direction("revenue"),"gross_margin_trend":delta("gross_margin"),"gross_margin_direction":direction("gross_margin",0.25),"operating_margin_trend":delta("operating_margin"),"operating_margin_direction":direction("operating_margin",0.25),"net_margin_trend":delta("net_margin"),"net_margin_direction":direction("net_margin",0.25),
        "debt_ratio_trend":delta("debt_ratio"),"debt_ratio_direction":direction("debt_ratio",0.25),"current_ratio_trend":delta("current_ratio"),"current_ratio_direction":direction("current_ratio",1.0),"consecutive_losses":streak(enriched,lambda row:isinstance(row.get("eps"),(int,float)) and row["eps"]<0),
        "negative_ocf_streak":streak(enriched,lambda row:isinstance(row.get("operating_cash_flow"),(int,float)) and row["operating_cash_flow"]<0),"negative_fcf_streak":streak(enriched,lambda row:isinstance(row.get("free_cash_flow"),(int,float)) and row["free_cash_flow"]<0),
        "ttm_operating_cash_flow":latest.get("ttm_operating_cash_flow"),"ttm_free_cash_flow":latest.get("ttm_free_cash_flow"),"profit_margin_stability":stability,
        "profitability_direction":"improving" if delta("gross_margin") is not None and delta("operating_margin") is not None and delta("gross_margin")>0 and delta("operating_margin")>0 else "deteriorating" if delta("gross_margin") is not None and delta("operating_margin") is not None and delta("gross_margin")<0 and delta("operating_margin")<0 else "stable"}}


def apply_financial_summary(data: dict[str,Any], trends: dict[str,Any]) -> None:
    if not trends.get("applicable") or not trends.get("records"): return
    latest=trends["records"][-1]; fundamentals=data["fundamentals"]; period=latest["period_end"]
    fields={"eps":("eps","TWD"),"eps_growth":("eps_yoy","percent"),"roe":("roe","percent"),"revenue":("revenue","thousand_TWD"),"gross_margin":("gross_margin","percent"),"operating_margin":("operating_margin","percent"),"net_margin":("net_margin","percent"),"debt_ratio":("debt_ratio","percent"),"current_ratio":("current_ratio","percent"),"operating_cash_flow":("operating_cash_flow","thousand_TWD"),"free_cash_flow":("free_cash_flow","thousand_TWD")}
    for target,(source,unit) in fields.items(): fundamentals[target]={"value":latest.get(source),"data_date":period,"unit":unit,"note":"MOPS 多期合併財報；現金流為累計值還原之單季數" if target in {"operating_cash_flow","free_cash_flow"} else None}
    fundamentals["report_period"]=f"{latest['fiscal_year']}Q{latest['quarter']}"; fundamentals["report_date"]=period


def health_values(data: dict[str, Any]) -> dict[str, Any]:
    f=data["fundamentals"]; summary=data.get("financial_trends",{}).get("summary",{})
    return {"revenue_yoy":f.get("revenue_yoy",{}).get("value"),"roe":f.get("roe",{}).get("value"),"debt_ratio":f.get("debt_ratio",{}).get("value"),"current_ratio":f.get("current_ratio",{}).get("value"),**summary,
        "pe_percentile":data.get("historical_valuation",{}).get("pe",{}).get("5y",{}).get("current_percentile"),"pb_percentile":data.get("historical_valuation",{}).get("pb",{}).get("5y",{}).get("current_percentile"),"yield_percentile":data.get("historical_valuation",{}).get("dividend_yield",{}).get("5y",{}).get("current_percentile")}


def make_health(data: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if data["profile"]["instrument_type"]!="company": return {"applicable":False,"reason":"ETF／非一般公司不適用公司健檢","rules_version":config["version"],"categories":{}}
    values=health_values(data); dates={key:value.get("data_date") for key,value in data["fundamentals"].items() if isinstance(value,dict)}
    labels={"revenue_yoy":"月營收 YoY","eps_yoy":"EPS YoY","eps_growth_streak":"EPS 連續成長期數","quarter_revenue_yoy":"季營收 YoY","roe":"ROE","gross_margin_trend":"毛利率近四季變化","operating_margin_trend":"營業利益率近四季變化","consecutive_losses":"連續虧損季度","eps_decline_streak":"EPS 連續衰退期數","negative_ocf_streak":"連續負 OCF 季數","negative_fcf_streak":"連續負 FCF 季數","debt_ratio_trend":"負債比近四季變化","debt_ratio":"負債比","current_ratio":"流動比率","ttm_eps":"TTM EPS","profit_margin_stability":"近四季獲利率波動","ttm_operating_cash_flow":"TTM OCF","ttm_free_cash_flow":"TTM FCF","pe_percentile":"PE 歷史百分位","pb_percentile":"PB 歷史百分位","yield_percentile":"殖利率歷史百分位"}
    units={key:"percent" for key in ("revenue_yoy","eps_yoy","quarter_revenue_yoy","roe","gross_margin_trend","operating_margin_trend","debt_ratio_trend","debt_ratio","current_ratio","profit_margin_stability","pe_percentile","pb_percentile","yield_percentile")}
    units.update({"ttm_eps":"TWD","ttm_operating_cash_flow":"thousand_TWD","ttm_free_cash_flow":"thousand_TWD","eps_growth_streak":"quarters","consecutive_losses":"quarters","eps_decline_streak":"quarters","negative_ocf_streak":"quarters","negative_fcf_streak":"quarters"})
    output={}
    trend_date=data.get("financial_trends",{}).get("latest_period")
    for category,rules in config["categories"].items(): output[category]=[{"key":key,"label":labels[key],"value":values.get(key),"unit":units.get(key),"status":status(values.get(key),rule),"threshold":rule_text(rule),"data_date":dates.get(key) or trend_date or data["fundamentals"].get("report_date") or data["quote"].get("trade_date")} for key,rule in rules.items()]
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
    parser=argparse.ArgumentParser(); parser.add_argument("--symbol", action="append"); options=parser.parse_args()
    config=json.loads(RULE_PATH.read_text(encoding="utf-8")); summary_rules=json.loads(SUMMARY_RULE_PATH.read_text(encoding="utf-8")); snapshot=json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["stocks"]
    count=0
    for path in STOCK_DIR.glob("*.json"):
        if path.name in {"index.json","industry_snapshot.json","peer_rankings.json","build_stats.json"}: continue
        payload=json.loads(path.read_text(encoding="utf-8")); data=payload["data"]
        if options.symbol and data["profile"]["symbol"] not in options.symbol: continue
        if data["profile"]["instrument_type"]=="company":
            history_path=HISTORY_DIR/f"{data['profile']['symbol']}_valuation.json"
            observations=json.loads(history_path.read_text(encoding="utf-8"))["data"]["observations"] if history_path.exists() else []
            data["historical_valuation"]={field:{"current":data["valuation"][field]["value"],"3y":distribution(observations,field,data["valuation"][field]["value"],3),"5y":distribution(observations,field,data["valuation"][field]["value"],5),"source_frequency":"monthly_last_available_trading_day"} for field in ("pe","pb","dividend_yield")}
            financial_path=FINANCIAL_DIR/f"{data['profile']['symbol']}.json"
            financial_rows=json.loads(financial_path.read_text(encoding="utf-8"))["data"]["quarters"] if financial_path.exists() else []
            data["financial_trends"]=financial_trends(financial_rows)
            apply_financial_summary(data,data["financial_trends"])
        else:
            data["historical_valuation"]={"applicable":False,"reason":"ETF 不強制套用公司歷史估值"}
            data["financial_trends"]={"applicable":False,"reason":"ETF 不適用公司財務分析"}
        data["chips"]["analysis"]=chips_analysis(data["chips"]["history"])
        data["health_v2"]=make_health(data,config)
        data["industry_comparison"]=industry_comparison(data,snapshot,config["minimum_industry_samples"])
        data["analysis_summary"]=build_analysis_summary(data,summary_rules,snapshot)
        atomic(path,payload); count+=1
    print(f"Stock analysis updated: {count} caches")
    return 0


if __name__=="__main__": raise SystemExit(main())
