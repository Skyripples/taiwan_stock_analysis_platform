"""Build period-aligned industry snapshots, rankings, and per-stock analysis."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from config import PROJECT_ROOT
from update_stock_data import OfficialClient, URLS, atomic_json, build_stock, index_rows, safe_table


STOCK_DIR = PROJECT_ROOT / "data" / "stocks"
FINANCIAL_DIR = STOCK_DIR / "financials"
SNAPSHOT_PATH = STOCK_DIR / "industry_snapshot.json"
RANKING_PATH = STOCK_DIR / "peer_rankings.json"
MINIMUM_SAMPLE = 5

METRICS = {
    "pe": {"label":"PE","category":"valuation","direction":"lower","period":None},
    "pb": {"label":"PB","category":"valuation","direction":"lower","period":None},
    "dividend_yield": {"label":"殖利率","category":"valuation","direction":"higher","period":None},
    "revenue_yoy": {"label":"營收 YoY","category":"growth","direction":"higher","period":"revenue_period"},
    "eps_yoy": {"label":"EPS YoY","category":"growth","direction":"higher","period":"multi_period"},
    "eps": {"label":"EPS","category":"profitability","direction":"higher","period":"financial_period"},
    "roe": {"label":"ROE","category":"profitability","direction":"higher","period":"financial_period"},
    "gross_margin": {"label":"毛利率","category":"profitability","direction":"higher","period":"financial_period"},
    "operating_margin": {"label":"營業利益率","category":"profitability","direction":"higher","period":"financial_period"},
    "net_margin": {"label":"稅後淨利率","category":"profitability","direction":"higher","period":"financial_period"},
    "debt_ratio": {"label":"負債比","category":"safety","direction":"lower","period":"financial_period"},
    "current_ratio": {"label":"流動比率","category":"safety","direction":"context","period":"financial_period"},
    "ttm_operating_cash_flow": {"label":"TTM OCF","category":"safety","direction":"higher","period":"multi_period"},
    "ttm_free_cash_flow": {"label":"TTM FCF","category":"safety","direction":"higher","period":"multi_period"},
    "ttm_eps": {"label":"TTM EPS","category":"profitability","direction":"higher","period":"multi_period"},
}
RANKING_METRICS = ("roe","eps","revenue_yoy","pe","pb")


def args() -> argparse.Namespace:
    parser=argparse.ArgumentParser(description="Build official industry peer rankings")
    parser.add_argument("--refresh-snapshot",action="store_true")
    parser.add_argument("--delay",type=float,default=.2)
    return parser.parse_args()


def atomic(path:Path,payload:Any)->None:
    path.parent.mkdir(parents=True,exist_ok=True); temporary=path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w",encoding="utf-8",newline="\n") as handle:
            json.dump(payload,handle,ensure_ascii=False,indent=2,allow_nan=False);handle.write("\n");handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


def valid(value:Any)->bool:
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def metric_value(item:dict[str,Any],key:str)->Any:
    value=item.get(key)
    return None if key=="pe" and valid(value) and value<=0 else value


def merge_non_null(base:dict[str,Any],new:dict[str,Any])->dict[str,Any]:
    output=dict(base)
    for key,value in new.items():
        if value is not None and value!="":output[key]=value
    return output


def refresh_snapshot(delay:float)->list[dict[str,Any]]:
    old={row["symbol"]:row for row in json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")).get("stocks",[])} if SNAPSHOT_PATH.exists() else {}
    index=json.loads((STOCK_DIR/"index.json").read_text(encoding="utf-8"))["stocks"]
    client=OfficialClient(delay); raw={key:safe_table(client,key) for key in URLS}
    keys={"twse_profile":"公司代號","twse_quote":"Code","twse_valuation":"Code","twse_revenue":"公司代號","twse_eps":"公司代號","twse_profitability":"公司代號","twse_income":"公司代號","twse_balance":"公司代號","tpex_profile":"SecuritiesCompanyCode","tpex_quote":"SecuritiesCompanyCode","tpex_valuation":"SecuritiesCompanyCode","tpex_revenue":"公司代號","tpex_eps":"SecuritiesCompanyCode","tpex_income":"SecuritiesCompanyCode","tpex_balance":"SecuritiesCompanyCode"}
    tables={name:index_rows(rows,keys[name]) for name,rows in raw.items()}; output=[]
    for item in index:
        if item.get("instrument_type")!="company":continue
        try:
            compact=build_stock(item["symbol"],item["market"],tables,[])["data"]; f=compact["fundamentals"];v=compact["valuation"]
            row={"symbol":item["symbol"],"name":item["name"],"market":item["market"],"industry":compact["profile"]["industry"],"instrument_type":"company",
                "pe":v["pe"]["value"],"pb":v["pb"]["value"],"dividend_yield":v["dividend_yield"]["value"],"valuation_date":v["pe"]["data_date"],
                **{key:f[key]["value"] for key in ("eps","roe","revenue_yoy","gross_margin","operating_margin","net_margin","debt_ratio","current_ratio")},
                "financial_period":f["report_period"],"financial_date":f["report_date"],"revenue_period":f["revenue_yoy"]["data_date"]}
            output.append(merge_non_null(old.get(item["symbol"],{}),row))
        except Exception:
            if item["symbol"] in old:output.append(old[item["symbol"]])
    if len(output)<1000:raise RuntimeError(f"industry snapshot unexpectedly small: {len(output)}")
    return output


def overlay_multi_period(rows:list[dict[str,Any]])->None:
    by_symbol={row["symbol"]:row for row in rows}
    for path in FINANCIAL_DIR.glob("*.json"):
        symbol=path.stem
        if symbol not in by_symbol:continue
        records=json.loads(path.read_text(encoding="utf-8"))["data"]["quarters"]
        if len(records)<8:continue
        latest=records[-1]; same_year=[row for row in records if row["fiscal_year"]==latest["fiscal_year"] and row["quarter"]<=latest["quarter"]]
        income={key:sum(row[key] for row in same_year) for key in ("eps","revenue","gross_profit","operating_income","net_income") if all(valid(row.get(key)) for row in same_year)}
        equity=latest.get("total_equity"); annualizer=4/latest["quarter"]
        row=by_symbol[symbol]
        if len(income)==5:
            row.update({"eps":income["eps"],"gross_margin":income["gross_profit"]/income["revenue"]*100,"operating_margin":income["operating_income"]/income["revenue"]*100,"net_margin":income["net_income"]/income["revenue"]*100,"roe":income["net_income"]/equity*annualizer*100 if equity else None})
        row.update({"debt_ratio":latest.get("debt_ratio"),"current_ratio":latest.get("current_ratio"),"financial_period":f"{latest['fiscal_year']} Q{latest['quarter']}","financial_date":latest["period_end"],"multi_period":latest["period_end"],
            "eps_yoy":(latest["eps"]-records[-5]["eps"])/abs(records[-5]["eps"])*100 if len(records)>=5 and records[-5]["eps"] else None,
            "ttm_eps":sum(item["eps"] for item in records[-4:]),"ttm_operating_cash_flow":sum(item["operating_cash_flow"] for item in records[-4:]),"ttm_free_cash_flow":sum(item["free_cash_flow"] for item in records[-4:])})


def comparison(values:list[dict[str,Any]],current:dict[str,Any],key:str)->dict[str,Any]:
    spec=METRICS[key]; period_key=spec["period"]; period=current.get(period_key) if period_key else None
    same_industry=[row for row in values if row.get("industry")==current.get("industry")]
    mismatch=sum(1 for row in same_industry if period_key and row.get(period_key)!=period)
    eligible=[row for row in same_industry if (not period_key or row.get(period_key)==period) and valid(metric_value(row,key))]
    current_value=metric_value(current,key)
    data_date=current.get("valuation_date") if spec["category"]=="valuation" else current.get("revenue_period") if key=="revenue_yoy" else current.get("financial_date")
    base={"company_value":current_value,"industry_sample_size":len(same_industry),"industry_median":None,"percentile":None,"rank":None,"total_ranked":len(eligible),"relative_status":"unavailable","comparison_direction":spec["direction"],"comparison_period":period,"data_date":data_date,"period_mismatch_excluded":mismatch}
    if len(eligible)<MINIMUM_SAMPLE or not valid(current_value) or current not in eligible:return base
    numbers=[metric_value(row,key) for row in eligible]; base["industry_median"]=round(median(numbers),6)
    if spec["direction"]=="higher":base["rank"]=1+sum(value>current_value for value in numbers);base["percentile"]=sum(value<=current_value for value in numbers)/len(numbers)*100
    elif spec["direction"]=="lower":base["rank"]=1+sum(value<current_value for value in numbers);base["percentile"]=sum(value>=current_value for value in numbers)/len(numbers)*100
    else:base["percentile"]=sum(value<=current_value for value in numbers)/len(numbers)*100
    base["percentile"]=round(base["percentile"],4)
    if spec["direction"]!="context":
        p=base["percentile"];base["relative_status"]="leading" if p>=90 else "above_average" if p>=60 else "average" if p>=40 else "below_average" if p>=10 else "lagging"
    return base


def ranking(values:list[dict[str,Any]],current:dict[str,Any],key:str)->dict[str,Any]:
    result=comparison(values,current,key);spec=METRICS[key];period_key=spec["period"];period=current.get(period_key) if period_key else None
    eligible=[row for row in values if row.get("industry")==current.get("industry") and (not period_key or row.get(period_key)==period) and valid(metric_value(row,key))]
    reverse=spec["direction"]=="higher";eligible.sort(key=lambda row:(metric_value(row,key),row["symbol"]),reverse=reverse)
    entries=[]
    for row in eligible[:10]:
        peer=comparison(values,row,key);entries.append({"symbol":row["symbol"],"name":row["name"],"value":metric_value(row,key),"rank":peer["rank"],"percentile":peer["percentile"]})
    current_entry=next((entry for entry in entries if entry["symbol"]==current["symbol"]),None)
    if current_entry is None and valid(result["company_value"]):current_entry={"symbol":current["symbol"],"name":current["name"],"value":result["company_value"],"rank":result["rank"],"percentile":result["percentile"]}
    return {"metric":key,"label":spec["label"],"top10":entries,"current_company":current_entry,"sample_size":result["total_ranked"],"comparison_period":period}


def peer_analysis(rows:list[dict[str,Any]],current:dict[str,Any])->dict[str,Any]:
    categories={name:{} for name in ("valuation","growth","profitability","safety")}
    for key,spec in METRICS.items():categories[spec["category"]][key]=comparison(rows,current,key)
    return {"applicable":True,"industry":current["industry"],"industry_company_count":sum(row["industry"]==current["industry"] for row in rows),"data_date":current.get("financial_date") or current.get("valuation_date"),"minimum_sample_size":MINIMUM_SAMPLE,"status_rules":{"leading":"percentile >= 90","above_average":"60 <= percentile < 90","average":"40 <= percentile < 60","below_average":"10 <= percentile < 40","lagging":"percentile < 10","unavailable":"sample < 5, missing value, period mismatch, or context-only metric"},"categories":categories,"rankings":{key:ranking(rows,current,key) for key in RANKING_METRICS}}


def main()->int:
    options=args(); existing=json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["stocks"] if SNAPSHOT_PATH.exists() else []
    needs_refresh=options.refresh_snapshot or not existing or "gross_margin" not in existing[0]
    rows=refresh_snapshot(options.delay) if needs_refresh else existing;overlay_multi_period(rows)
    updated=datetime.now(timezone.utc).isoformat(timespec="seconds");atomic(SNAPSHOT_PATH,{"updated_at":updated,"version":"2.0","schema":{"metrics":METRICS,"minimum_sample_size":MINIMUM_SAMPLE},"stocks":rows})
    industries={}
    for industry,group in sorted(((name,[row for row in rows if row["industry"]==name]) for name in {row["industry"] for row in rows}),key=lambda item:item[0]):
        financial_periods=Counter(row.get("financial_period") for row in group if row.get("financial_period"));revenue_periods=Counter(row.get("revenue_period") for row in group if row.get("revenue_period"))
        rankings={}
        for key in RANKING_METRICS:
            spec=METRICS[key];period_key=spec["period"];representative=dict(group[0])
            if period_key=="financial_period" and financial_periods:representative[period_key]=financial_periods.most_common(1)[0][0]
            if period_key=="revenue_period" and revenue_periods:representative[period_key]=revenue_periods.most_common(1)[0][0]
            rankings[key]=ranking(rows,representative,key)
        industries[industry]={"company_count":len(group),"financial_periods":dict(financial_periods),"revenue_periods":dict(revenue_periods),"rankings":rankings}
    by_symbol={row["symbol"]:row for row in rows}
    atomic(RANKING_PATH,{"updated_at":updated,"version":"2.0","rules":{"minimum_sample_size":MINIMUM_SAMPLE,"metric_definitions":METRICS},"industries":industries})
    count=0
    for path in STOCK_DIR.glob("*.json"):
        if path.name in {"index.json","industry_snapshot.json","peer_rankings.json","build_stats.json"}:continue
        payload=json.loads(path.read_text(encoding="utf-8"));data=payload.get("data",{});profile=data.get("profile",{});symbol=profile.get("symbol")
        data["peer_analysis"]={"snapshot":"./data/stocks/industry_snapshot.json","rankings":"./data/stocks/peer_rankings.json","symbol":symbol} if profile.get("instrument_type")=="company" and symbol in by_symbol else {"applicable":False,"reason":"ETF／非一般公司不適用公司同業排名"}
        atomic(path,payload);count+=1
    print(f"Peer analysis updated: stocks={count} industries={len(industries)} snapshot={len(rows)}")
    return 0


if __name__=="__main__":raise SystemExit(main())
