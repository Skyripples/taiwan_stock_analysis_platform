"""Fetch completed regular-session TX futures quotes from official TAIFEX data."""
from __future__ import annotations
import logging, math, re
from calendar import monthcalendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from providers.taifex_night_futures_provider import _NightMarketParser
from io_utils import atomic_write_json

ROOT=Path(__file__).resolve().parents[1]; OUTPUT=ROOT/"data"/"futures"/"tx_futures.json"
SOURCE="https://www.taifex.com.tw/cht/3/futDailyMarketReport"; TAIPEI=timezone(timedelta(hours=8)); LOGGER=logging.getLogger("tx-futures")

def client():
    session=requests.Session(); retry=Retry(total=2,backoff_factor=.8,status_forcelist=(429,500,502,503,504),allowed_methods=frozenset({"POST"})); session.mount("https://",HTTPAdapter(max_retries=retry)); session.headers.update({"User-Agent":"taiwan-stock-analysis-platform/3.22","Accept":"text/html,application/xhtml+xml","Accept-Language":"zh-TW,zh;q=0.9"}); return session

def number(value:Any,integer=False):
    text=str(value).replace(",","").strip()
    if text in {"","-","--","---"}: return None
    negative="▼" in text; positive="▲" in text; numeric=re.sub(r"[^0-9.+-]","",text)
    if not numeric: return None
    result=float(numeric)
    if negative: result=-abs(result)
    elif positive: result=abs(result)
    if not math.isfinite(result): return None
    return int(result) if integer else (int(result) if result.is_integer() else result)

def third_wednesday(year:int,month:int):
    days=[week[2] for week in monthcalendar(year,month) if week[2]]; return date(year,month,days[2])

def select_contracts(rows:list[list[str]],trade_date:date,limit=2):
    indexes={"product":0,"month":1,"open":2,"high":3,"low":4,"close":5,"change":6,"change_percent":7,"volume":10,"settlement":11,"open_interest":12}
    for row in rows:
        headers=[re.sub(r"[\s*]+","",cell) for cell in row]
        if "契約" not in headers or "結算價" not in headers or "未沖銷契約量" not in headers: continue
        def position(name): return headers.index(name)
        volume_name="合計成交量" if "合計成交量" in headers else "成交量"
        indexes={"product":position("契約"),"month":position("到期月份(週別)"),"open":position("開盤價"),"high":position("最高價"),"low":position("最低價"),"close":position("最後成交價"),"change":position("漲跌價"),"change_percent":position("漲跌%"),"volume":position(volume_name),"settlement":position("結算價"),"open_interest":position("未沖銷契約量")}; break
    contracts=[]
    for row in rows:
        if len(row)<=max(indexes.values()) or row[indexes["product"]].strip()!="TX" or not re.fullmatch(r"\d{6}",row[indexes["month"]].strip()): continue
        month=row[indexes["month"]].strip(); expiry=third_wednesday(int(month[:4]),int(month[4:]));
        if expiry<trade_date: continue
        contract={"contract_month":f"{month[:4]}-{month[4:]}","contract_code":month,"expiry_date":expiry.isoformat(),"open":number(row[indexes["open"]]),"high":number(row[indexes["high"]]),"low":number(row[indexes["low"]]),"close":number(row[indexes["close"]]),"change":number(row[indexes["change"]]),"change_percent":number(row[indexes["change_percent"]]),"volume":number(row[indexes["volume"]],True),"settlement_price":number(row[indexes["settlement"]]),"open_interest":number(row[indexes["open_interest"]],True)}
        if any(contract[key] is None for key in ("open","high","low","close","volume","open_interest")): continue
        contracts.append(contract)
    contracts.sort(key=lambda item:item["contract_code"])
    if not contracts: raise ValueError("TAIFEX response contains no unexpired monthly TX contract")
    return contracts[:limit]

def fetch(session,now=None):
    today=(now or datetime.now(TAIPEI)).astimezone(TAIPEI).date()
    for offset in range(15):
        query=today-timedelta(days=offset); query_text=query.strftime("%Y/%m/%d")
        response=session.post(SOURCE,data={"queryType":"2","marketCode":"0","MarketCode":"0","dateaddcnt":"","commodity_id":"TX","commodity_id2":"","queryDate":query_text},timeout=(10,40)); response.raise_for_status(); response.encoding="utf-8"
        parser=_NightMarketParser(); parser.feed(response.text)
        if parser.query_date!=query_text: continue
        try: contracts=select_contracts(parser.rows,query)
        except ValueError: continue
        return {"trade_date":query.isoformat(),"contracts":contracts}
    raise ValueError("TAIFEX returned no completed TX regular-session quote in lookback")

def build_payload(raw,now=None):
    stamp=(now or datetime.now(timezone.utc)).isoformat(); contracts=raw["contracts"]
    return {"updated_at":stamp,"fetched_at":stamp,"provider":"TAIFEX","dataset":"tx_futures","version":"1.0","product_code":"TX","product_name":"臺股期貨","session":"regular","market_status":"最近交易日行情","trade_date":raw["trade_date"],"units":{"price":"index_point","volume":"contract","open_interest":"contract"},"source":SOURCE,"front_month":contracts[0],"next_month":contracts[1] if len(contracts)>1 else None,"contracts":contracts}

def validate(payload):
    if payload.get("dataset")!="tx_futures" or payload.get("product_code")!="TX" or payload.get("session")!="regular": raise ValueError("invalid TX metadata")
    datetime.strptime(payload["trade_date"],"%Y-%m-%d"); contracts=payload.get("contracts")
    if not isinstance(contracts,list) or not contracts or payload.get("front_month")!=contracts[0]: raise ValueError("invalid TX contracts")
    codes=[item.get("contract_code") for item in contracts]
    if codes!=sorted(codes) or len(codes)!=len(set(codes)): raise ValueError("TX contracts are not nearest unique months")
    for item in contracts:
        if not re.fullmatch(r"\d{6}",str(item.get("contract_code",""))): raise ValueError("invalid contract month")
        required=(item.get("open"),item.get("high"),item.get("low"),item.get("close"),item.get("volume"),item.get("open_interest"))
        if any(value is None for value in required) or item["high"]<max(item["open"],item["close"]) or item["low"]>min(item["open"],item["close"]): raise ValueError("invalid TX OHLC or activity")

def atomic_write(payload,output=OUTPUT):
    atomic_write_json(output,payload)

def main():
    logging.basicConfig(level=logging.INFO,format="%(levelname)s | %(message)s")
    try:
        payload=build_payload(fetch(client())); validate(payload); atomic_write(payload); LOGGER.info("TX futures updated | trade_date=%s | front=%s",payload["trade_date"],payload["front_month"]["contract_month"]); return 0
    except Exception as exc: LOGGER.error("TX futures update failed; existing valid file was preserved: %s",exc); return 1

if __name__=="__main__": raise SystemExit(main())
