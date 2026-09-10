"""Build a lightweight Taiwan ETF comparison snapshot from official exchanges."""
from __future__ import annotations
import calendar, json, logging, math, os, tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

ROOT=Path(__file__).resolve().parents[1]; INDEX=ROOT/"data"/"stocks"/"index.json"; OUTPUT=ROOT/"data"/"funds"/"funds.json"
TWSE_LATEST="https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"; TPEX_LATEST="https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
TWSE_HISTORY="https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"; TPEX_HISTORY="https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
PERIODS={"1m":1,"3m":3,"6m":6,"1y":12}; LOGGER=logging.getLogger("funds")

def client():
    s=requests.Session(); retry=Retry(total=2,backoff_factor=.8,status_forcelist=(429,500,502,503,504),allowed_methods=frozenset({"GET"})); s.mount("https://",HTTPAdapter(max_retries=retry)); s.headers.update({"User-Agent":"taiwan-stock-analysis-platform/3.21","Accept":"application/json"}); return s

def get(session,url,**kwargs):
    try: return session.get(url,**kwargs)
    except requests.exceptions.SSLError:
        if "tpex.org.tw" not in url: raise
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return session.get(url,verify=False,**kwargs)

def number(value:Any):
    try:
        result=float(str(value).replace(",","").replace("+","").strip()); return result if math.isfinite(result) else None
    except (TypeError,ValueError): return None

def roc_date(value:str):
    raw=str(value).strip().replace("/","")
    if len(raw)==7 and raw.isdigit(): return f"{int(raw[:3])+1911:04d}-{raw[3:5]}-{raw[5:7]}"
    raise ValueError(f"invalid ROC date: {value}")

def subtract_months(value:date,months:int):
    month_index=value.year*12+value.month-1-months; year,month_zero=divmod(month_index,12); month=month_zero+1
    return date(year,month,min(value.day,calendar.monthrange(year,month)[1]))

def load_universe():
    payload=json.loads(INDEX.read_text(encoding="utf-8")); funds={}
    for item in payload.get("stocks",[]):
        if item.get("active") and item.get("instrument_type")=="ETF":
            market=item.get("market"); category="上市 ETF" if market=="TWSE" else "上櫃 ETF" if market=="TPEx" else "ETF"
            funds[item["symbol"]]={"symbol":item["symbol"],"fund_code":item["symbol"],"name":item.get("name"),"market":market,"category":category,"asset_type":"ETF","issuer":None,"nav":None,"expense_ratio":None,"management_fee":None}
    if not funds: raise ValueError("official stock universe contains no active ETFs")
    return funds

def fetch_latest(session):
    result={}
    response=get(session,TWSE_LATEST,timeout=(10,45)); response.raise_for_status()
    for row in response.json():
        close=number(row.get("ClosingPrice"))
        if close is not None: result[str(row.get("Code","")).strip()]={"price":close,"source_date":roc_date(row["Date"])}
    response=get(session,TPEX_LATEST,timeout=(10,45)); response.raise_for_status()
    for row in response.json():
        close=number(row.get("Close"))
        if close is not None: result[str(row.get("SecuritiesCompanyCode","")).strip()]={"price":close,"source_date":roc_date(row["Date"])}
    if not result: raise ValueError("official exchanges returned no current prices")
    return result,max(item["source_date"] for item in result.values())

def parse_prices(payload,symbols,close_index):
    prices={}
    for table in payload.get("tables",[]):
        for row in table.get("data",[]):
            if isinstance(row,list) and len(row)>close_index and str(row[0]).strip() in symbols:
                close=number(row[close_index])
                if close is not None: prices[str(row[0]).strip()]=close
    return prices

def fetch_anchor(session,target,market,symbols):
    for offset in range(11):
        day=target-timedelta(days=offset)
        if market=="TWSE":
            response=get(session,TWSE_HISTORY,params={"date":day.strftime("%Y%m%d"),"type":"ALLBUT0999","response":"json"},timeout=(10,45)); response.raise_for_status(); payload=response.json(); prices=parse_prices(payload,symbols,8) if payload.get("stat")=="OK" else {}
        else:
            response=get(session,TPEX_HISTORY,params={"date":day.strftime("%Y/%m/%d"),"response":"json"},timeout=(10,45)); response.raise_for_status(); payload=response.json(); prices=parse_prices(payload,symbols,2) if payload.get("date")==day.strftime("%Y%m%d") else {}
        if prices: return day.isoformat(),prices
    raise ValueError(f"{market} has no official quote near {target.isoformat()}")

def build_payload(session,now=None):
    universe=load_universe(); latest,data_date=fetch_latest(session); latest_day=date.fromisoformat(data_date); anchors={key:{} for key in PERIODS}
    for key,months in PERIODS.items():
        for market in ("TWSE","TPEx"):
            symbols={symbol for symbol,fund in universe.items() if fund["market"]==market}
            if not symbols: continue
            anchor_date,prices=fetch_anchor(session,subtract_months(latest_day,months),market,symbols)
            anchors[key].update({symbol:(anchor_date,price) for symbol,price in prices.items()})
    funds=[]
    for symbol,fund in universe.items():
        current=latest.get(symbol)
        if not current: continue
        returns={}; return_dates={}
        for key in PERIODS:
            anchor=anchors[key].get(symbol); returns[key]=round((current["price"]/anchor[1]-1)*100,4) if anchor and anchor[1]>0 else None; return_dates[key]=anchor[0] if anchor else None
        funds.append({**fund,"price":current["price"],"data_date":current["source_date"],"returns":returns,"return_base_dates":return_dates,"source":"TWSE" if fund["market"]=="TWSE" else "TPEx"})
    if not funds: raise ValueError("no ETF matched the official universe and quotes")
    timestamp=(now or datetime.now(timezone.utc)).isoformat()
    return {"updated_at":timestamp,"fetched_at":timestamp,"data_date":data_date,"provider":"TWSE / TPEx","dataset":"taiwan_etf_comparison","version":"1.0","unit":{"price":"TWD","return":"percent","fee":"percent"},"sources":{"universe":"data/stocks/index.json (TWSE / TPEx official universe)","twse":TWSE_LATEST,"tpex":TPEX_LATEST},"funds":sorted(funds,key=lambda item:item["symbol"])}

def validate(payload):
    if payload.get("dataset")!="taiwan_etf_comparison" or not payload.get("funds"): raise ValueError("invalid funds payload")
    seen=set()
    for fund in payload["funds"]:
        if not fund.get("symbol") or fund["symbol"] in seen or fund.get("price") is None: raise ValueError("invalid or duplicate ETF row")
        seen.add(fund["symbol"])
        if any(value is not None and not math.isfinite(value) for value in fund.get("returns",{}).values()): raise ValueError("invalid ETF return")

def atomic_write(payload,output=OUTPUT):
    output.parent.mkdir(parents=True,exist_ok=True); fd,temporary=tempfile.mkstemp(prefix=output.name,suffix=".tmp",dir=output.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as stream: json.dump(payload,stream,ensure_ascii=False,indent=2); stream.write("\n")
        os.replace(temporary,output)
    except Exception:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise

def main():
    logging.basicConfig(level=logging.INFO,format="%(levelname)s | %(message)s")
    try:
        payload=build_payload(client()); validate(payload); atomic_write(payload); LOGGER.info("ETF comparison updated | funds=%d | data_date=%s",len(payload["funds"]),payload["data_date"]); return 0
    except Exception as exc: LOGGER.error("ETF comparison update failed; existing valid file was preserved: %s",exc); return 1

if __name__=="__main__": raise SystemExit(main())
