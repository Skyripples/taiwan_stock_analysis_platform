"""PC-only, resumable official stock-history backfill into gzip JSONL staging.

The command never writes PostgreSQL. Daily sources are requested once per market/date
and split by symbol; financial statements are the only symbol-specific source.
"""
from __future__ import annotations

import argparse, gzip, json, os, sys, tempfile, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
STAGING = PROJECT_ROOT / "data_lake" / "staging" / "stock_history"
INDEX = PROJECT_ROOT / "data" / "stocks" / "index.json"
TWSE_DAILY = "https://www.twse.com.tw/rwd/zh"
TPEX_DAILY = "https://www.tpex.org.tw/www/zh-tw"
DATASETS = ("revenue", "financials", "valuation", "chips")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill official stock history to local staging")
    parser.add_argument("--dataset", choices=(*DATASETS, "all"), default="all")
    parser.add_argument("--start"); parser.add_argument("--end")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--delay", type=float, default=.5)
    return parser.parse_args()


def range_for(options: argparse.Namespace) -> tuple[date,date]:
    end = date.fromisoformat(options.end) if options.end else date.today()
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


def fetch_valuation(client: Any, day: date, markets: dict[str,str]) -> list[dict[str,Any]]:
    from update_stock_data import number
    output=[]; compact=day.strftime("%Y%m%d"); slash=day.strftime("%Y/%m/%d")
    if "TWSE" in markets.values():
        payload=client.json(f"{TWSE_DAILY}/afterTrading/BWIBBU_d",params={"date":compact,"selectType":"ALL","response":"json"})
        for row in payload.get("data",[]):
            if row and markets.get(str(row[0]).strip())=="TWSE": output.append({"symbol":str(row[0]).strip(),"valuation_date":day.isoformat(),"dividend_yield":number(row[3]),"pe":number(row[5]),"pb":number(row[6]),"source":"TWSE BWIBBU_d","source_date":day.isoformat()})
    if "TPEx" in markets.values():
        payload=client.json(f"{TPEX_DAILY}/aftertrading/peratio_analysis/pera_result.php",params={"d":f"{day.year-1911:03d}/{day.month:02d}/{day.day:02d}","l":"zh-tw","o":"json","s":"0"})
        table=(payload.get("tables") or [{}])[0]
        for row in table.get("data",[]):
            if row and markets.get(str(row[0]).strip())=="TPEx": output.append({"symbol":str(row[0]).strip(),"valuation_date":day.isoformat(),"pe":number(row[2]),"dividend_yield":number(row[5]),"pb":number(row[6]),"source":"TPEx pera_result","source_date":day.isoformat()})
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


def main() -> int:
    options=arguments(); start,end=range_for(options); markets,_=universe(options.symbols)
    selected=DATASETS if options.dataset=="all" else (options.dataset,)
    plan={}
    for name in selected:
        existing=list((STAGING/name).glob("*.jsonl.gz"))
        planned_units=len(markets) if name=="financials" else len(eligible_revenue_months(start,end) if name=="revenue" else weekdays(start,end))
        plan[name]={"start":start.isoformat(),"end":end.isoformat(),"symbols":len(markets),"existing_partitions":len(existing),"planned_units":planned_units,"gap_only":not options.refresh}
    if options.dry_run:
        print(json.dumps({"dry_run":True,"network_requests":0,"plan":plan},ensure_ascii=False,indent=2)); return 0
    from update_stock_data import OfficialClient, fetch_chips_history
    from update_stock_financials import MopsClient, fetch_symbol, recent_quarters
    client=OfficialClient(options.delay); downloaded=duplicates=skipped_existing=0; failures=[]
    for dataset in selected:
        if dataset=="financials":
            quarters=recent_quarters(max(1,options.years*4))
            for symbol in markets:
                path=partition(dataset,symbol); existing=[] if options.refresh else read_rows(path)
                if existing and options.resume: continue
                try:
                    rows=fetch_symbol(MopsClient(options.delay),symbol,quarters,existing)
                    for row in rows: row.update({"symbol":symbol,"source_date":row.get("available_date"),"fetched_at":datetime.now(timezone.utc).isoformat()})
                    write_rows(path,rows,"symbol fiscal_year quarter statement_scope".split()); downloaded+=len(rows)
                except Exception as exc: failures.append({"dataset":dataset,"symbol":symbol,"error":str(exc)})
            continue
        jobs=eligible_revenue_months(start,end) if dataset=="revenue" else weekdays(start,end)
        for job in jobs:
            key=job.strftime("%Y-%m"); path=partition(dataset,key); existing=[] if options.refresh else read_rows(path)
            if dataset=="revenue" and options.resume and not options.refresh and revenue_partition_complete(existing,job,set(markets)):
                skipped_existing+=sum(1 for row in existing if row.get("symbol") in markets and row.get("revenue_month")==job.isoformat())
                continue
            covered={row.get("source_date") for row in existing}
            if job.isoformat() in covered and not options.refresh: continue
            try:
                if dataset=="valuation": rows=fetch_valuation(client,job,markets)
                elif dataset=="revenue": rows=fetch_revenue(client,job,markets)
                else:
                    fetched=fetch_chips_history(client,[job.isoformat()],set(markets),markets)
                    rows=[{**row,"symbol":symbol,"source":"TWSE/TPEx official institutional and margin","source_date":job.isoformat()} for symbol,items in fetched.items() for row in items]
                for row in rows: row["fetched_at"]=datetime.now(timezone.utc).isoformat()
                before=len(existing); write_rows(path,existing+rows,("symbol","revenue_month" if dataset=="revenue" else "valuation_date" if dataset=="valuation" else "trade_date")); downloaded+=len(rows); duplicates+=max(0,before+len(rows)-len({tuple(item.get(k) for k in (("symbol","revenue_month") if dataset=="revenue" else ("symbol","valuation_date") if dataset=="valuation" else ("symbol","trade_date"))) for item in existing+rows}))
                if not rows: failures.append({"dataset":dataset,"date":job.isoformat(),"error":"official-empty"})
            except Exception as exc: failures.append({"dataset":dataset,"date":job.isoformat(),"error":str(exc)})
    summary={"downloaded_rows":downloaded,"skipped_existing":skipped_existing,"date_range":[start.isoformat(),end.isoformat()],"symbols":len(markets),"duplicates":duplicates,"missing_dates":[item.get("date") for item in failures if item.get("error")=="official-empty"],"failures":failures,"completed_at":datetime.now(timezone.utc).isoformat()}
    STAGING.mkdir(parents=True,exist_ok=True); (STAGING/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2)); return 0 if not failures else 2


if __name__=="__main__": raise SystemExit(main())
