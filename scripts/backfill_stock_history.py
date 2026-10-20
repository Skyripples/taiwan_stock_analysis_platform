"""PC-only, resumable official stock-history backfill into gzip JSONL staging.

The command never writes PostgreSQL. Daily sources are requested once per market/date
and split by symbol; financial statements are the only symbol-specific source.
"""
from __future__ import annotations

import argparse, gzip, json, os, sys, tempfile, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from zoneinfo import ZoneInfo

from config import PROJECT_ROOT
from io_utils import atomic_write_json
from trading_calendar import get_trading_days_in_range
STAGING = PROJECT_ROOT / "data_lake" / "staging" / "stock_history"
INDEX = PROJECT_ROOT / "data" / "stocks" / "index.json"
TWSE_DAILY = "https://www.twse.com.tw/rwd/zh"
TPEX_DAILY = "https://www.tpex.org.tw/www/zh-tw"
TPEX_VALUATION = f"{TPEX_DAILY}/afterTrading/peQryDate"
DATASETS = ("revenue", "financials", "valuation", "chips")
TAIPEI = ZoneInfo("Asia/Taipei")
VALUATION_CLOSE_HOUR = 15
VALUATION_CLOSE_MINUTE = 30


class ValuationSourceError(RuntimeError):
    """The official source did not return a usable response."""


class ValuationOfficialEmpty(RuntimeError):
    """The official source has no observation for the requested session."""


class ValuationOfficialPartial(RuntimeError):
    """One official market is empty while another returned valid rows."""
    def __init__(self, missing_markets: list[str], rows: list[dict[str,Any]]):
        self.missing_markets=missing_markets;self.rows=rows
        super().__init__("partial official valuation response: "+",".join(missing_markets))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill official stock history to local staging")
    parser.add_argument("--dataset", choices=(*DATASETS, "all"), default="all")
    parser.add_argument("--start"); parser.add_argument("--end")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--audit-only", action="store_true", help="Audit existing chips staging without network")
    parser.add_argument("--delay", type=float, default=.5)
    return parser.parse_args()


def range_for(options: argparse.Namespace) -> tuple[date,date]:
    end = date.fromisoformat(options.end) if options.end else datetime.now(TAIPEI).date()
    start = date.fromisoformat(options.start) if options.start else date(end.year-options.years,end.month,end.day)
    if start > end: raise ValueError("start cannot exceed end")
    return start,end


def universe(symbols: list[str] | None) -> tuple[dict[str,str],dict[str,str]]:
    rows=json.loads(INDEX.read_text(encoding="utf-8"))["stocks"]
    wanted=set(symbols or [])
    rows=[row for row in rows if row.get("active",True) and row.get("instrument_type")=="company" and (not wanted or row["symbol"] in wanted)]
    return ({row["symbol"]:row["market"] for row in rows},{row["symbol"]:row.get("name") for row in rows})


def partition(dataset: str, key: str) -> Path: return STAGING/dataset/f"{key}.jsonl.gz"


def read_rows(path: Path) -> list[dict[str,Any]]:
    if not path.exists(): return []
    with gzip.open(path,"rt",encoding="utf-8") as stream: return [json.loads(line) for line in stream if line.strip()]


def write_rows(path: Path, rows: list[dict[str,Any]], keys: tuple[str,...]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    unique={tuple(row.get(key) for key in keys):row for row in rows}
    descriptor,name=tempfile.mkstemp(prefix=f".{path.name}.",suffix=".tmp",dir=path.parent); os.close(descriptor)
    try:
        with gzip.open(name,"wt",encoding="utf-8",newline="\n") as stream:
            for row in sorted(unique.values(),key=lambda item:tuple(str(item.get(key) or "") for key in keys)):
                stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+"\n")
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def weekdays(start: date,end: date) -> list[date]:
    result=[]; cursor=start
    while cursor<=end:
        if cursor.weekday()<5: result.append(cursor)
        cursor+=timedelta(days=1)
    return result


def valuation_trading_days(start: date, end: date) -> tuple[list[date], list[date]]:
    """Resolve valuation jobs through the shared PIT-aware calendar helper."""
    return get_trading_days_in_range(start,end)


def eligible_valuation_days(start: date,end: date,now: datetime|None=None)->tuple[list[date],list[date],list[date]]:
    """Return completed sessions, closed dates, and sessions not completed in Taipei."""
    current=(now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    sessions,closed=valuation_trading_days(start,end)
    completed=[];not_completed=[]
    close_time=current.replace(hour=VALUATION_CLOSE_HOUR,minute=VALUATION_CLOSE_MINUTE,second=0,microsecond=0)
    for day in sessions:
        if day<current.date() or (day==current.date() and current>=close_time):completed.append(day)
        else:not_completed.append(day)
    return completed,closed,not_completed


def eligible_completed_trading_days(start: date,end: date,now: datetime|None=None)->tuple[list[date],list[date],list[date]]:
    """Shared fail-closed completed-session planner for daily official datasets."""
    return eligible_valuation_days(start,end,now)


def valuation_status_path()->Path:return STAGING/"valuation_status.json"


def load_valuation_status()->dict[str,Any]:
    path=valuation_status_path()
    if not path.exists():return {"dates":{}}
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) and isinstance(value.get("dates"),dict) else {"dates":{}}
    except (OSError,json.JSONDecodeError):return {"dates":{}}


def valuation_market_coverage(rows:list[dict[str,Any]],day:date)->set[str]:
    result=set()
    for row in rows:
        if row.get("valuation_date")!=day.isoformat():continue
        source=str(row.get("source") or "")
        if source.startswith("TWSE "):result.add("TWSE")
        elif source.startswith("TPEx "):result.add("TPEx")
    return result


def months(start: date, end: date) -> list[date]:
    cursor=date(start.year,start.month,1); output=[]
    while cursor<=end:
        output.append(cursor)
        cursor=date(cursor.year+(cursor.month==12),1 if cursor.month==12 else cursor.month+1,1)
    return output


def eligible_revenue_months(start: date, end: date, today: date | None = None) -> list[date]:
    """Return completed calendar months only; the current month is not historical revenue."""
    current=today or date.today()
    current_month=date(current.year,current.month,1)
    return [month for month in months(start,end) if month < current_month]


def revenue_partition_complete(rows: list[dict[str,Any]], month: date, symbols: set[str]) -> bool:
    """A revenue partition is complete when every requested symbol has that month."""
    present={
        str(row.get("symbol"))
        for row in rows
        if row.get("revenue_month")==month.isoformat()
    }
    return bool(symbols) and symbols.issubset(present)


def valuation_partition_complete(rows: list[dict[str,Any]], day: date, symbols: set[str]) -> bool:
    present={str(row.get("symbol")) for row in rows if row.get("valuation_date")==day.isoformat()}
    return bool(symbols) and symbols.issubset(present)


def roc_date(value: Any) -> date | None:
    text=str(value or "").strip().replace("-", "/")
    parts=text.split("/")
    try:
        if len(parts)==3: return date(int(parts[0])+1911,int(parts[1]),int(parts[2]))
    except ValueError:
        return None
    return None


def official_json(client: Any, url: str, params: dict[str,str]) -> dict[str,Any]:
    """Validate content type before JSON parsing; fixture clients may expose json()."""
    session=getattr(client,"session",None)
    if session is None:
        payload=client.json(url,params=params)
        if not isinstance(payload,dict): raise ValuationSourceError("official response is not a JSON object")
        return payload
    error: Exception | None=None
    for attempt in range(3):
        wait=max(0,float(getattr(client,"delay",0))-(time.monotonic()-float(getattr(client,"last_request",0))))
        if wait: time.sleep(wait)
        try:
            try:
                response=session.get(url,params=params,timeout=45)
            except requests.exceptions.SSLError:
                response=session.get(url,params=params,timeout=45,verify=False)
            client.last_request=time.monotonic()
            response.raise_for_status()
            content_type=response.headers.get("content-type","").lower()
            if "json" not in content_type:
                raise ValuationSourceError(f"official response is not JSON ({content_type or 'unknown content type'})")
            payload=response.json()
            if not isinstance(payload,dict): raise ValuationSourceError("official response is not a JSON object")
            return payload
        except ValuationSourceError:
            raise
        except Exception as exc:
            error=exc
            if attempt<2: time.sleep(2**attempt)
    raise ValuationSourceError(f"official source request failed: {type(error).__name__}")


def parse_tpex_valuation(payload: dict[str,Any], day: date, markets: dict[str,str]) -> list[dict[str,Any]]:
    from update_stock_data import number
    tables=payload.get("tables")
    if not isinstance(tables,list) or not tables: raise ValuationSourceError("TPEx response has no tables")
    table=tables[0]
    source_day=roc_date(table.get("date"))
    if source_day!=day: raise ValuationOfficialEmpty(f"TPEx has no observation for {day.isoformat()}")
    fields=table.get("fields") or []
    required=("股票代號","本益比","殖利率(%)","股價淨值比")
    if not all(field in fields for field in required): raise ValuationSourceError("TPEx valuation fields changed")
    indexes={field:fields.index(field) for field in required}
    output=[]
    for row in table.get("data") or []:
        if not isinstance(row,list) or len(row)<=max(indexes.values()): continue
        symbol=str(row[indexes["股票代號"]]).strip()
        if markets.get(symbol)!="TPEx": continue
        output.append({"symbol":symbol,"valuation_date":day.isoformat(),
                       "pe":number(row[indexes["本益比"]]),
                       "dividend_yield":number(row[indexes["殖利率(%)"]]),
                       "pb":number(row[indexes["股價淨值比"]]),
                       "source":"TPEx peQryDate","source_date":day.isoformat()})
    return output


def fetch_valuation(client: Any, day: date, markets: dict[str,str]) -> list[dict[str,Any]]:
    from update_stock_data import number
    output=[]; compact=day.strftime("%Y%m%d"); empty=[]
    if "TWSE" in markets.values():
        payload=official_json(client,f"{TWSE_DAILY}/afterTrading/BWIBBU_d",{"date":compact,"selectType":"ALL","response":"json"})
        if payload.get("stat")!="OK" or not payload.get("data"): empty.append("TWSE")
        for row in payload.get("data",[]):
            if row and markets.get(str(row[0]).strip())=="TWSE": output.append({"symbol":str(row[0]).strip(),"valuation_date":day.isoformat(),"dividend_yield":number(row[3]),"pe":number(row[5]),"pb":number(row[6]),"source":"TWSE BWIBBU_d","source_date":day.isoformat()})
    if "TPEx" in markets.values():
        payload=official_json(client,TPEX_VALUATION,{"date":f"{day.year-1911:03d}/{day.month:02d}/{day.day:02d}","cate":""})
        try: output.extend(parse_tpex_valuation(payload,day,markets))
        except ValuationOfficialEmpty: empty.append("TPEx")
    requested={market for market in markets.values() if market in {"TWSE","TPEx"}}
    if requested and requested.issubset(empty): raise ValuationOfficialEmpty(f"no official valuation for {day.isoformat()}")
    if empty: raise ValuationOfficialPartial(empty,output)
    return output


def fetch_revenue(client: Any, month: date, markets: dict[str,str]) -> list[dict[str,Any]]:
    from update_stock_financials import TableParser
    output=[]
    from update_stock_data import number
    for market,section in (("TWSE","sii"),("TPEx","otc")):
        url=f"https://mopsov.twse.com.tw/nas/t21/{section}/t21sc03_{month.year-1911}_{month.month}_0.html"
        wait=client.delay-(time.monotonic()-client.last_request)
        if wait>0: time.sleep(wait)
        response=client.session.get(url,timeout=45); client.last_request=time.monotonic(); response.raise_for_status()
        response.encoding=response.apparent_encoding or "big5"
        parser=TableParser(); parser.feed(response.text)
        for row in parser.rows:
            symbol=str(row[0]).strip() if row else ""
            if not symbol.isalnum() or markets.get(symbol)!=market or len(row)<9: continue
            output.append({"symbol":symbol,"revenue_month":month.isoformat(),"revenue":number(row[2]),"revenue_mom":number(row[5]),"revenue_yoy":number(row[6]),"source":f"MOPS t21sc03 {section}","source_date":None})
    return output


CHIPS_FIELDS={
    "institutional": ("foreign_buy","foreign_sell","foreign_net","investment_trust_buy","investment_trust_sell","investment_trust_net","dealer_buy","dealer_sell","dealer_net","institutional_total"),
    "margin": ("margin_balance","margin_change","short_balance","short_change"),
}
# Verified directly against TWSE on 2026-10-14. Both endpoints return the
# official no-data response for this session; they are not retryable gaps.
PERSISTENT_CHIPS_OFFICIAL_EMPTY={
    ("2026-07-10","TWSE","institutional"),
    ("2026-07-10","TWSE","margin"),
}


def chips_status_path()->Path:return STAGING/"chips_status.json"


def load_chips_status()->dict[str,Any]:
    path=chips_status_path()
    if not path.exists():return {"partitions":{}}
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) and isinstance(value.get("partitions"),dict) else {"partitions":{}}
    except (OSError,json.JSONDecodeError):return {"partitions":{}}


def chips_partition_key(day:date,market:str,component:str)->str:
    return f"{day.isoformat()}|{market}|{component}"


def merge_chips_component(existing:list[dict[str,Any]],incoming:dict[str,dict[str,Any]],day:date,market:str,component:str,fetched_at:str)->tuple[list[dict[str,Any]],int]:
    """NULL-safe merge: a failed/empty component can never erase valid staged values."""
    by_key={(str(row.get("symbol")),str(row.get("trade_date"))):dict(row) for row in existing}
    created=0
    for symbol,values in incoming.items():
        key=(symbol,day.isoformat()); row=by_key.get(key)
        if row is None:
            row={"symbol":symbol,"trade_date":day.isoformat()};by_key[key]=row;created+=1
        for field in CHIPS_FIELDS[component]:
            if values.get(field) is not None:row[field]=values[field]
            elif field not in row:row[field]=None
        sources=set(row.get("source_components") or [])
        sources.add(f"{market}:{component}")
        row.update({"market":market,"source":"TWSE/TPEx official","source_components":sorted(sources),"source_date":day.isoformat(),"available_at":fetched_at,"fetched_at":fetched_at})
    return list(by_key.values()),created


def audit_chips_staging(start:date,end:date,markets:dict[str,str])->dict[str,Any]:
    rows=[]
    for path in sorted((STAGING/"chips").glob("*.jsonl.gz")):rows.extend(read_rows(path))
    rows=[row for row in rows if start.isoformat()<=str(row.get("trade_date"))<=end.isoformat() and row.get("symbol") in markets]
    keys=[(row.get("symbol"),row.get("trade_date")) for row in rows]
    dates=sorted({str(row.get("trade_date")) for row in rows})
    component_dates={}
    partial=[]
    for market in ("TWSE","TPEx"):
        for component,fields in CHIPS_FIELDS.items():
            component_dates[f"{market}:{component}"]=sorted({str(row["trade_date"]) for row in rows if markets.get(str(row.get("symbol")))==market and any(row.get(field) is not None for field in fields)})
    for row in rows:
        missing=[component for component,fields in CHIPS_FIELDS.items() if not any(row.get(field) is not None for field in fields)]
        if missing:partial.append({"symbol":row.get("symbol"),"trade_date":row.get("trade_date"),"missing_components":missing})
    sessions,_,_=eligible_completed_trading_days(start,end)
    expected={day.isoformat() for day in sessions}
    missing_component_dates={key:sorted(expected-set(value)) for key,value in component_dates.items()}
    return {"rows":len(rows),"unique_rows":len(set(keys)),"duplicate_keys":len(keys)-len(set(keys)),"symbols":len({row.get('symbol') for row in rows}),"first_date":dates[0] if dates else None,"last_date":dates[-1] if dates else None,"component_dates":component_dates,"missing_component_dates":missing_component_dates,"partial_rows":partial}


def audit_financials_staging(markets:dict[str,str],quarter_count:int)->dict[str,Any]:
    from update_stock_financials import recent_quarters
    rows=[]
    for path in sorted((STAGING/"financials").glob("*.jsonl.gz")):rows.extend(read_rows(path))
    rows=[row for row in rows if row.get("symbol") in markets]
    keys=[(row.get("symbol"),row.get("fiscal_year"),row.get("quarter"),row.get("statement_scope","consolidated")) for row in rows]
    periods=sorted({f"{row.get('fiscal_year')}Q{row.get('quarter')}" for row in rows})
    expected={f"{year}Q{quarter}" for year,quarter in recent_quarters(quarter_count)}
    coverage={symbol:sorted({f"{row.get('fiscal_year')}Q{row.get('quarter')}" for row in rows if row.get("symbol")==symbol}) for symbol in markets}
    return {"rows":len(rows),"unique_rows":len(set(keys)),"duplicate_keys":len(keys)-len(set(keys)),"symbols":len({row.get('symbol') for row in rows}),"min_quarter":periods[0] if periods else None,"max_quarter":periods[-1] if periods else None,"quarter_coverage":coverage,"missing_quarters":{symbol:sorted(expected-set(found)) for symbol,found in coverage.items()},"source_coverage":{str(source):sum(row.get("source")==source for row in rows) for source in sorted({row.get("source") for row in rows},key=str)}}


def financial_dependency_quarters(requested:list[tuple[int,int]])->list[tuple[int,int]]:
    """Return exact same-year dependencies for cumulative cash flow and Q4 income.

    Q2/Q3 need earlier quarters to de-cumulate cash flows; Q4 additionally
    needs Q1-Q3 to convert annual income into a standalone quarter.
    """
    return sorted({(year,prior) for year,quarter in requested for prior in range(1,quarter+1)})


def financial_component_path(symbol:str,year:int,quarter:int,component:str)->Path:
    return STAGING/"financials"/"_components"/f"symbol={symbol}"/f"{year}Q{quarter}"/f"{component}.json.gz"


def read_financial_component(path:Path)->dict[str,Any]|None:
    if not path.exists():return None
    try:
        with gzip.open(path,"rt",encoding="utf-8") as stream:value=json.load(stream)
        if not isinstance(value,dict) or value.get("status")!="complete" or not isinstance(value.get("rows"),list):return None
        return value
    except (OSError,json.JSONDecodeError):return None


def write_financial_component(path:Path,payload:dict[str,Any])->None:
    """Crash-safe component checkpoint; NULL values do not affect completion."""
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,name=tempfile.mkstemp(prefix=f".{path.stem}.",suffix=".tmp",dir=path.parent);os.close(descriptor)
    try:
        with gzip.open(name,"wt",encoding="utf-8",newline="\n") as stream:
            json.dump(payload,stream,ensure_ascii=False,allow_nan=False);stream.write("\n")
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def load_or_fetch_financial_component(client:Any,symbol:str,year:int,quarter:int,component:str,endpoint:str,*,refresh:bool=False)->list[list[str]]:
    path=financial_component_path(symbol,year,quarter,component)
    cached=None if refresh else read_financial_component(path)
    if cached is not None:return cached["rows"]
    rows=client.rows(endpoint,symbol,year,quarter)
    # MopsClient.rows has already parsed and validated the HTML table here.
    write_financial_component(path,{"version":1,"status":"complete","symbol":symbol,"fiscal_year":year,"quarter":quarter,"component":component,"endpoint":endpoint,"source":"MOPS","rows":rows,"fetched_at":datetime.now(timezone.utc).isoformat()})
    return rows


def financial_status_path()->Path:return STAGING/"financials_status.json"


def load_financial_status()->dict[str,Any]:
    path=financial_status_path()
    if not path.exists():return {"version":1,"quarters":{}}
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) and isinstance(value.get("quarters"),dict) else {"version":1,"quarters":{}}
    except (OSError,json.JSONDecodeError):return {"version":1,"quarters":{}}


def financial_quarter_key(symbol:str,year:int,quarter:int)->str:
    return f"{symbol}|{year}Q{quarter}"


def update_financial_status(status:dict[str,Any],symbol:str,year:int,quarter:int,state:str,**details:Any)->None:
    entry={"symbol":symbol,"fiscal_year":year,"quarter":quarter,"status":state,
           "updated_at":datetime.now(timezone.utc).isoformat(),**details}
    status.setdefault("version",1);status.setdefault("quarters",{})[financial_quarter_key(symbol,year,quarter)]=entry
    atomic_write_json(financial_status_path(),status)


def financial_source_status(exc:Exception)->str:
    message=str(exc).lower()
    if "statement table is empty" in message or "returned no statement" in message:
        return "official_empty"
    return "retryable_error"


def process_financial_symbol(client:Any,symbol:str,calculation_quarters:list[tuple[int,int]],
                             requested:set[tuple[int,int]],existing:list[dict[str,Any]],
                             status:dict[str,Any],*,refresh:bool=False)->dict[str,Any]:
    """Process and persist financial statements one quarter at a time."""
    from update_stock_financials import (ENDPOINTS,FinancialDependencyMissing,
                                         derive_quarter,parse_quarter_statements)
    path=partition("financials",symbol)
    by_key={(row.get("fiscal_year"),row.get("quarter"),row.get("statement_scope","consolidated")):dict(row) for row in existing}
    context={(int(row["fiscal_year"]),int(row["quarter"])):dict(row) for row in existing
             if row.get("fiscal_year") is not None and row.get("quarter") is not None}
    downloaded=skipped=duplicates=0;failures=[]
    for year,quarter in calculation_quarters:
        period=(year,quarter);canonical_key=(year,quarter,"consolidated")
        if period in requested and canonical_key in by_key and not refresh:
            skipped+=1
            update_financial_status(status,symbol,year,quarter,"success",canonical_written=True,reused=True)
            continue

        statements={};source_failed=False
        for component,endpoint in ENDPOINTS.items():
            try:
                statements[component]=load_or_fetch_financial_component(
                    client,symbol,year,quarter,component,endpoint,refresh=refresh
                )
            except Exception as exc:
                state=financial_source_status(exc)
                update_financial_status(status,symbol,year,quarter,state,component=component,error=str(exc),
                                        retryable=state=="retryable_error")
                failures.append({"dataset":"financials","symbol":symbol,"fiscal_year":year,"quarter":quarter,
                                 "component":component,"status":state,"error":str(exc),
                                 "retryable":state=="retryable_error"})
                source_failed=True;break
        if source_failed:continue

        raw,missing=parse_quarter_statements(symbol,year,quarter,statements)
        # Preserve partial parsed values as dependency context. A later quarter
        # may still safely use its cumulative cash-flow/equity basis.
        context[period]=raw
        if missing:
            update_financial_status(status,symbol,year,quarter,"parser_schema_error",missing_fields=missing)
            failures.append({"dataset":"financials","symbol":symbol,"fiscal_year":year,"quarter":quarter,
                             "status":"parser_schema_error","error":"missing required fields",
                             "missing_fields":missing,"retryable":False})
            continue
        try:
            row,dependency_flags=derive_quarter(symbol,raw,context)
        except FinancialDependencyMissing as exc:
            update_financial_status(status,symbol,year,quarter,"dependency_missing",dependencies=exc.dependencies)
            failures.append({"dataset":"financials","symbol":symbol,"fiscal_year":year,"quarter":quarter,
                             "status":"dependency_missing","error":str(exc),"dependencies":exc.dependencies,
                             "retryable":False})
            continue
        context[period]=row
        if period not in requested:
            update_financial_status(status,symbol,year,quarter,"success",canonical_written=False,
                                    dependency_only=True,dependency_flags=dependency_flags)
            continue

        stamp=datetime.now(timezone.utc).isoformat()
        row.update({"symbol":symbol,"statement_scope":"consolidated","fetched_at":row.get("fetched_at") or stamp})
        row.setdefault("source_date",row.get("period_end"));row.setdefault("available_at",None)
        existed=canonical_key in by_key
        by_key[canonical_key]=row
        write_rows(path,list(by_key.values()),("symbol","fiscal_year","quarter","statement_scope"))
        downloaded+=0 if existed else 1;duplicates+=1 if existed else 0
        update_financial_status(status,symbol,year,quarter,"success",canonical_written=True,
                                dependency_flags=dependency_flags)
    return {"downloaded_rows":downloaded,"skipped_existing":skipped,"duplicates":duplicates,"failures":failures}


def run_chips_backfill(options:argparse.Namespace,start:date,end:date,markets:dict[str,str],client:Any)->dict[str,Any]:
    from update_stock_data import CHIPS_COMPONENTS,fetch_chips_component
    jobs,closed,not_completed=eligible_completed_trading_days(start,end)
    status=load_chips_status(); downloaded=skipped=duplicates=0;failures=[];official_empty=[]
    requested_markets=sorted(set(markets.values())&{"TWSE","TPEx"})
    for day in jobs:
        path=partition("chips",day.strftime("%Y-%m"));existing=[] if options.refresh else read_rows(path)
        for market in requested_markets:
            selected={symbol for symbol,value in markets.items() if value==market}
            for component in CHIPS_COMPONENTS:
                key=chips_partition_key(day,market,component);previous=status["partitions"].get(key,{})
                if (day.isoformat(),market,component) in PERSISTENT_CHIPS_OFFICIAL_EMPTY:
                    status["partitions"][key]={"status":"official-empty","retryable":False,"verified_at":"2026-10-14","updated_at":datetime.now(timezone.utc).isoformat()}
                    official_empty.append({"date":day.isoformat(),"market":market,"component":component,"persistent":True,"retryable":False})
                    skipped+=1;atomic_write_json(chips_status_path(),status);continue
                if options.resume and not options.refresh and previous.get("status") in {"success","official-empty"}:
                    skipped+=1;continue
                fetched_at=datetime.now(timezone.utc).isoformat()
                try:
                    full=fetch_chips_component(client,market,component,day.isoformat())
                    if not full:
                        state="official-empty";official_empty.append({"date":day.isoformat(),"market":market,"component":component})
                    else:
                        filtered={symbol:row for symbol,row in full.items() if symbol in selected}
                        existing,created=merge_chips_component(existing,filtered,day,market,component,fetched_at)
                        write_rows(path,existing,("symbol","trade_date"));downloaded+=created
                        # A valid full-market response is successful even when an
                        # individual security has no row (suspension/ineligibility).
                        state="success"
                    status["partitions"][key]={"status":state,"rows":len(full),"updated_at":fetched_at}
                except Exception as exc:
                    status["partitions"][key]={"status":"retryable-error","error":str(exc),"updated_at":fetched_at}
                    failures.append({"dataset":"chips","date":day.isoformat(),"market":market,"component":component,"error":str(exc),"retryable":True})
                atomic_write_json(chips_status_path(),status)
    return {"downloaded_rows":downloaded,"skipped_existing":skipped,"skipped_non_trading_days":len(closed),"skipped_not_completed":len(not_completed),"duplicates":duplicates,"official_empty":official_empty,"failures":failures,"audit":audit_chips_staging(start,end,markets)}


def main() -> int:
    options=arguments(); start,end=range_for(options); markets,_=universe(options.symbols)
    selected=DATASETS if options.dataset=="all" else (options.dataset,)
    plan={}
    planned_not_completed=0
    for name in selected:
        existing=list((STAGING/name).glob("*.jsonl.gz"))
        if name in {"valuation","chips"}:
            planned_dates,_,not_completed=eligible_valuation_days(start,end);planned_not_completed=len(not_completed)
        else:planned_dates=weekdays(start,end)
        planned_units=len(markets) if name=="financials" else len(eligible_revenue_months(start,end) if name=="revenue" else planned_dates)
        plan[name]={"start":start.isoformat(),"end":end.isoformat(),"symbols":len(markets),"existing_partitions":len(existing),"planned_units":planned_units,"skipped_not_completed":len(not_completed) if name in {"valuation","chips"} else 0,"gap_only":not options.refresh}
    if getattr(options,"audit_only",False):
        report={"audit_only":True,"network_requests":0}
        if options.dataset=="financials":report["financials"]=audit_financials_staging(markets,max(1,options.years*4))
        else:report["chips"]=audit_chips_staging(start,end,markets)
        print(json.dumps(report,ensure_ascii=False,indent=2));return 0
    if options.dry_run:
        print(json.dumps({"dry_run":True,"network_requests":0,"plan":plan},ensure_ascii=False,indent=2)); return 0
    from update_stock_data import OfficialClient
    from update_stock_financials import MopsClient, recent_quarters
    client=OfficialClient(options.delay); downloaded=duplicates=skipped_existing=skipped_non_trading_days=skipped_not_completed=skipped_official_partial=0; failures=[];chips_official_empty=[]
    valuation_status=load_valuation_status();financial_status=load_financial_status()
    for dataset in selected:
        if dataset=="chips":
            result=run_chips_backfill(options,start,end,markets,client)
            downloaded+=result["downloaded_rows"];skipped_existing+=result["skipped_existing"]
            skipped_non_trading_days+=result["skipped_non_trading_days"];skipped_not_completed+=result["skipped_not_completed"]
            duplicates+=result["duplicates"];failures.extend(result["failures"])
            chips_official_empty.extend(result["official_empty"])
            continue
        if dataset=="financials":
            target_count=max(1,options.years*4)
            quarters=recent_quarters(target_count)
            requested=set(quarters)
            calculation_quarters=financial_dependency_quarters(quarters)
            financial_client=MopsClient(options.delay)
            for symbol in markets:
                path=partition(dataset,symbol); existing=[] if options.refresh else read_rows(path)
                result=process_financial_symbol(financial_client,symbol,calculation_quarters,requested,existing,
                                                financial_status,refresh=options.refresh)
                downloaded+=result["downloaded_rows"];skipped_existing+=result["skipped_existing"]
                duplicates+=result["duplicates"];failures.extend(result["failures"])
            continue
        if dataset=="revenue": jobs=eligible_revenue_months(start,end)
        elif dataset=="valuation":
            jobs,non_trading,not_completed=eligible_valuation_days(start,end)
            skipped_non_trading_days+=len(non_trading);skipped_not_completed+=len(not_completed)
        else: jobs=[job for job in weekdays(start,end) if job<=date.today()]
        for job in jobs:
            key=job.strftime("%Y-%m"); path=partition(dataset,key); existing=[] if options.refresh else read_rows(path)
            if dataset=="revenue" and options.resume and not options.refresh and revenue_partition_complete(existing,job,set(markets)):
                skipped_existing+=sum(1 for row in existing if row.get("symbol") in markets and row.get("revenue_month")==job.isoformat())
                continue
            if dataset=="valuation" and options.resume and not options.refresh:
                entry=valuation_status["dates"].get(job.isoformat(),{})
                if entry.get("status")=="official_partial":skipped_official_partial+=1;continue
                requested_markets={value for value in markets.values() if value in {"TWSE","TPEx"}}
                coverage=valuation_market_coverage(existing,job)
                if entry.get("status")=="complete" or requested_markets.issubset(coverage):
                    skipped_existing+=sum(1 for row in existing if row.get("symbol") in markets and row.get("valuation_date")==job.isoformat())
                    valuation_status["dates"][job.isoformat()]={"status":"complete","markets":sorted(coverage),"updated_at":datetime.now(timezone.utc).isoformat()}
                    atomic_write_json(valuation_status_path(),valuation_status);continue
            covered={row.get("source_date") for row in existing}
            if dataset!="valuation" and job.isoformat() in covered and not options.refresh: continue
            try:
                if dataset=="valuation": rows=fetch_valuation(client,job,markets)
                elif dataset=="revenue": rows=fetch_revenue(client,job,markets)
                else: rows=[]
                for row in rows: row["fetched_at"]=datetime.now(timezone.utc).isoformat()
                keys=("symbol","revenue_month") if dataset=="revenue" else ("symbol","valuation_date") if dataset=="valuation" else ("symbol","trade_date")
                existing_keys={tuple(item.get(k) for k in keys) for item in existing};incoming_keys={tuple(item.get(k) for k in keys) for item in rows}
                write_rows(path,existing+rows,keys);downloaded+=len(incoming_keys-existing_keys);duplicates+=len(incoming_keys&existing_keys)
                if dataset=="valuation":
                    coverage=valuation_market_coverage(existing+rows,job)
                    valuation_status["dates"][job.isoformat()]={"status":"complete","markets":sorted(coverage),"updated_at":datetime.now(timezone.utc).isoformat()}
                    atomic_write_json(valuation_status_path(),valuation_status)
                if not rows: failures.append({"dataset":dataset,"date":job.isoformat(),"error":"official-empty"})
            except ValuationOfficialEmpty:
                failures.append({"dataset":dataset,"date":job.isoformat(),"error":"official-empty","retryable":False})
            except ValuationOfficialPartial as exc:
                for row in exc.rows:row["fetched_at"]=datetime.now(timezone.utc).isoformat()
                keys=("symbol","valuation_date");existing_keys={tuple(item.get(k) for k in keys) for item in existing};incoming_keys={tuple(item.get(k) for k in keys) for item in exc.rows}
                write_rows(path,existing+exc.rows,keys);downloaded+=len(incoming_keys-existing_keys);duplicates+=len(incoming_keys&existing_keys)
                coverage=valuation_market_coverage(existing+exc.rows,job)
                valuation_status["dates"][job.isoformat()]={"status":"official_partial","markets":sorted(coverage),"missing_markets":exc.missing_markets,"updated_at":datetime.now(timezone.utc).isoformat()}
                atomic_write_json(valuation_status_path(),valuation_status)
                failures.append({"dataset":dataset,"date":job.isoformat(),"error":"official_partial","missing_markets":exc.missing_markets,"retryable":False})
            except Exception as exc: failures.append({"dataset":dataset,"date":job.isoformat(),"error":str(exc),"retryable":True})
    official_empty=[item.get("date") for item in failures if item.get("error")=="official-empty"]
    official_empty.extend(item for item in failures if item.get("status")=="official_empty")
    official_empty+=chips_official_empty
    source_failures=[item for item in failures if item.get("error")!="official-empty" and item.get("status")!="official_empty"]
    summary={"downloaded_rows":downloaded,"skipped_existing":skipped_existing,"skipped_non_trading_days":skipped_non_trading_days,"skipped_not_completed":skipped_not_completed,"skipped_official_partial":skipped_official_partial,"date_range":[start.isoformat(),end.isoformat()],"symbols":len(markets),"duplicates":duplicates,"missing_dates":[],"official_empty":official_empty,"failures":source_failures,"completed_at":datetime.now(timezone.utc).isoformat()}
    STAGING.mkdir(parents=True,exist_ok=True); (STAGING/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2)); return 0 if not source_failures else 2


if __name__=="__main__": raise SystemExit(main())
