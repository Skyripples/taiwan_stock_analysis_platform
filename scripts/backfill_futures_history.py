"""Plan or download official TAIFEX daily futures history into local staging."""
from __future__ import annotations

import argparse, gzip, json, os, random, tempfile, time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from config import PROJECT_ROOT
from futures_market import PRODUCTS, contract_metadata
from trading_calendar import get_trading_days_in_range
from io_utils import atomic_write_json
from update_tx_futures import SOURCE, select_contracts, _NightMarketParser

STAGING = PROJECT_ROOT / "data_lake" / "staging" / "futures_daily"
CHECKPOINT = STAGING / "checkpoint.json"
DEFAULT_DELAY = 2.5
BACKOFF_SECONDS = (5,10,20,40,60)
PRODUCT_LISTING_DATES = {
    # TAIFEX official launch date: Micro TAIEX Futures (TMF).
    "TMF": date(2024, 7, 29),
}
# Verified directly against TAIFEX. The daily endpoint returns no market rows
# for any Taiwan index futures size on this session; never synthesize values.
PERSISTENT_OFFICIAL_EMPTY = {
    ("TX", date(2026, 7, 10)),
    ("MTX", date(2026, 7, 10)),
    ("TMF", date(2026, 7, 10)),
}


class RateLimitExhausted(RuntimeError):
    """TAIFEX continued returning 429 after every controlled retry."""


class OfficialEmpty(RuntimeError):
    """The official page contains no usable table for the requested product/date."""


class RequestController:
    """Single-threaded throttle, 429 backoff, and cross-job cooldown."""
    def __init__(self,http,delay=DEFAULT_DELAY,*,sleep=time.sleep,clock=time.monotonic,jitter=random.uniform,
                 backoffs=BACKOFF_SECONDS,cooldown_after=3,cooldown_seconds=90):
        self.http=http;self.delay=max(0,float(delay));self.sleep=sleep;self.clock=clock;self.jitter=jitter
        self.backoffs=tuple(backoffs);self.cooldown_after=cooldown_after;self.cooldown_seconds=cooldown_seconds
        self.last_request=0.0;self.consecutive_429=0;self.retry_count=0;self.rate_limit_count=0

    def _throttle(self):
        remaining=self.delay-(self.clock()-self.last_request)
        if remaining>0:self.sleep(remaining)

    @staticmethod
    def _retry_after(response)->float:
        value=response.headers.get("Retry-After")
        if not value:return 0
        try:return max(0,float(value))
        except ValueError:
            try:return max(0,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
            except (TypeError,ValueError,OverflowError):return 0

    def post(self,url,**kwargs):
        for attempt in range(len(self.backoffs)+1):
            self._throttle();response=self.http.post(url,**kwargs);self.last_request=self.clock()
            if response.status_code!=429:
                self.consecutive_429=0;response.raise_for_status();return response
            self.rate_limit_count+=1;self.consecutive_429+=1
            if attempt>=len(self.backoffs):
                raise RateLimitExhausted("TAIFEX HTTP 429 retries exhausted")
            self.retry_count+=1
            wait=max(self.backoffs[attempt],self._retry_after(response))+self.jitter(0,.75)
            self.sleep(wait)
            if self.consecutive_429>=self.cooldown_after:
                self.sleep(self.cooldown_seconds+self.jitter(0,5));self.consecutive_429=0
        raise RateLimitExhausted("TAIFEX HTTP 429 retries exhausted")


def history_client():
    session=requests.Session()
    session.headers.update({"User-Agent":"taiwan-stock-analysis-platform/4.3 historical-backfill","Accept":"text/html,application/xhtml+xml","Accept-Language":"zh-TW,zh;q=0.9"})
    return session


def arguments():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products",nargs="+",choices=PRODUCTS,default=list(PRODUCTS))
    parser.add_argument("--start"); parser.add_argument("--end"); parser.add_argument("--years",type=int,default=5)
    parser.add_argument("--resume",action="store_true"); parser.add_argument("--dry-run",action="store_true")
    parser.add_argument("--delay",type=float,default=DEFAULT_DELAY)
    return parser.parse_args()


def requested_range(options):
    end=date.fromisoformat(options.end) if options.end else date.today()
    start=date.fromisoformat(options.start) if options.start else end.replace(year=end.year-options.years)
    if start>end: raise ValueError("start cannot exceed end")
    return start,end


def path_for(product:str,year:int)->Path: return STAGING/f"product={product}"/f"year={year}"/"daily.jsonl.gz"


def read_rows(path:Path)->list[dict]:
    if not path.exists(): return []
    with gzip.open(path,"rt",encoding="utf-8") as stream:return [json.loads(line) for line in stream if line.strip()]


def write_rows(path:Path,rows:list[dict])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    unique={(row["trade_date"],row["product"],row["contract"],row["session"]):row for row in rows}
    descriptor,name=tempfile.mkstemp(prefix=".futures-",suffix=".tmp",dir=path.parent);os.close(descriptor)
    try:
        with gzip.open(name,"wt",encoding="utf-8",newline="\n") as stream:
            for row in sorted(unique.values(),key=lambda item:(item["trade_date"],item["contract"],item["session"])):
                stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+"\n")
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def successful_dates(products:list[str],start:date,end:date)->dict[str,set[str]]:
    return {product:{row["trade_date"] for year in range(start.year,end.year+1) for row in read_rows(path_for(product,year))} for product in products}


def plan_jobs(products:list[str],sessions:list[date],existing:dict[str,set[str]],resume:bool)->list[tuple[str,date]]:
    return [
        (product,day) for product in products for day in sessions
        if day>=PRODUCT_LISTING_DATES.get(product,date.min)
        and (product,day) not in PERSISTENT_OFFICIAL_EMPTY
        and not(resume and day.isoformat() in existing[product])
    ]


def pre_listing_count(products:list[str],sessions:list[date])->int:
    """Count ineligible product/session pairs without treating them as gaps."""
    return sum(day<PRODUCT_LISTING_DATES.get(product,date.min) for product in products for day in sessions)


def persistent_empty_entries(products:list[str],sessions:list[date])->list[dict]:
    selected=set(products);available=set(sessions)
    return [{"product":product,"date":day.isoformat(),"status":"official-empty","retryable":False,"persistent":True,"error":"TAIFEX official daily source contains no market rows"} for product,day in sorted(PERSISTENT_OFFICIAL_EMPTY) if product in selected and day in available]


def fetch_day(http,day:date,product:str)->list[dict]:
    query=day.strftime("%Y/%m/%d")
    response=http.post(SOURCE,data={"queryType":"2","marketCode":"0","MarketCode":"0","dateaddcnt":"","commodity_id":product,"commodity_id2":"","queryDate":query},timeout=(10,40))
    response.encoding="utf-8";parser=_NightMarketParser();parser.feed(response.text)
    if parser.query_date!=query:raise OfficialEmpty(f"TAIFEX response date mismatch for {product} {day.isoformat()}")
    if not any(row and row[0].strip()==product for row in parser.rows):
        raise OfficialEmpty(f"TAIFEX response contains no {product} market rows")
    stamp=datetime.now(timezone.utc).isoformat(); output=[]
    for rank,item in enumerate(select_contracts(parser.rows,day,limit=6,product=product)):
        expiry=date.fromisoformat(item["expiry_date"]);meta=contract_metadata(day,expiry)
        output.append({"trade_date":day.isoformat(),"product":product,"contract":product+item["contract_code"],"contract_month":item["contract_month"],"session":"regular",
          "open":item["open"],"high":item["high"],"low":item["low"],"close":item["close"],"settlement_price":item["settlement_price"],"volume":item["volume"],"open_interest":item["open_interest"],
          "expiry_date":item["expiry_date"],**meta,"contract_rank":"front" if rank==0 else "next" if rank==1 else "quarter",
          "source":SOURCE,"source_date":day.isoformat(),"available_at":f"{day.isoformat()}T15:30:00+08:00","fetched_at":stamp})
    return output


def main()->int:
    options=arguments();start,end=requested_range(options);sessions,closed=get_trading_days_in_range(start,end)
    existing_dates=successful_dates(options.products,start,end)
    jobs=plan_jobs(options.products,sessions,existing_dates,options.resume)
    skipped_pre_listing=pre_listing_count(options.products,sessions)
    persistent_empty=persistent_empty_entries(options.products,sessions)
    if options.dry_run:
        print(json.dumps({"dry_run":True,"network_requests":0,"products":options.products,"sessions":len(sessions),"skipped_non_trading_days":len(closed),"skipped_pre_listing":skipped_pre_listing,"skipped_persistent_official_empty":len(persistent_empty),"persistent_official_empty":persistent_empty,"planned_requests":len(jobs)},ensure_ascii=False,indent=2));return 0
    controller=RequestController(history_client(),options.delay);downloaded=0;failures=[]
    for product,day in jobs:
        try:
            rows=fetch_day(controller,day,product)
            target=path_for(product,day.year);write_rows(target,read_rows(target)+rows);downloaded+=len(rows)
        except OfficialEmpty as exc:failures.append({"product":product,"date":day.isoformat(),"status":"official-empty","retryable":True,"error":str(exc)})
        except RateLimitExhausted as exc:failures.append({"product":product,"date":day.isoformat(),"status":"failed","retryable":True,"error":str(exc)})
        except Exception as exc:failures.append({"product":product,"date":day.isoformat(),"status":"failed","retryable":True,"error":str(exc)})
        atomic_write_json(CHECKPOINT,{"status":"running","last_product":product,"last_date":day.isoformat(),"downloaded_rows":downloaded,"retry_count":controller.retry_count,"rate_limit_count":controller.rate_limit_count,"failures":failures,"updated_at":datetime.now(timezone.utc).isoformat()})
    failures_by_product={product:sum(item["product"]==product for item in failures) for product in options.products}
    summary={"downloaded_rows":downloaded,"products":options.products,"date_range":[start.isoformat(),end.isoformat()],"skipped_existing":sum(len(existing_dates[product]&{day.isoformat() for day in sessions}) for product in options.products),"skipped_non_trading_days":len(closed),"skipped_pre_listing":skipped_pre_listing,"skipped_persistent_official_empty":len(persistent_empty),"persistent_official_empty":persistent_empty,"retry_count":controller.retry_count,"rate_limit_count":controller.rate_limit_count,"failures":failures,"failures_by_product":failures_by_product,"completed_at":datetime.now(timezone.utc).isoformat()}
    STAGING.mkdir(parents=True,exist_ok=True);atomic_write_json(STAGING/"summary.json",summary);atomic_write_json(CHECKPOINT,{**summary,"status":"partial" if failures else "complete"})
    print(json.dumps(summary,ensure_ascii=False,indent=2));return 0 if not any(item["status"]=="failed" for item in failures) else 2

if __name__=="__main__":raise SystemExit(main())
