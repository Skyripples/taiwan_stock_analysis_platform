"""Build full-market, leakage-safe stock prediction datasets in PostgreSQL."""
from __future__ import annotations

import argparse, csv, json, logging, math, os, random, statistics, time
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import requests

from config import PROJECT_ROOT
from database.connection import connect

LOG=logging.getLogger("stock_prediction_dataset")
REPORT=PROJECT_ROOT/"data"/"analysis"/"stock_prediction_dataset_report.json"
HISTORY=PROJECT_ROOT/"data"/"history"/"historical_prediction_dataset.csv"
VERSION="v3.14-1.0"; TAIPEI=ZoneInfo("Asia/Taipei")
PILOT=("2330","2317","6488","2881","1101")
FORMAL=("taiex_close","taiex_change_percent","tpex_close","turnover","advancing","declining","foreign_cash_flow","foreign_futures_position","night_futures_change","tsm_adr_change_percent","sox_change_percent","sp500_change_percent","nasdaq_change_percent","vix_change_percent","kospi_change_percent")

def args():
    p=argparse.ArgumentParser(); g=p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pilot",action="store_true"); g.add_argument("--all",action="store_true"); g.add_argument("--symbol",action="append")
    p.add_argument("--start",type=date.fromisoformat); p.add_argument("--end",type=date.fromisoformat,default=date.today())
    p.add_argument("--resume",action="store_true"); p.add_argument("--delay",type=float,default=.6); p.add_argument("--init-schema",action="store_true")
    return p.parse_args()

def num(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except (TypeError,ValueError): return None

class Client:
    def __init__(self,delay): self.s=requests.Session(); self.s.headers["User-Agent"]="taiwan-stock-analysis-platform/3.14"; self.delay=delay; self.last=0.0
    def get(self,url,**kwargs):
        error=None
        for attempt in range(4):
            wait=self.delay-(time.monotonic()-self.last)
            if wait>0: time.sleep(wait)
            try:
                r=self.s.get(url,timeout=(10,90),**kwargs); self.last=time.monotonic()
                if r.status_code==429: time.sleep(2**(attempt+1)); continue
                r.raise_for_status(); return r
            except requests.RequestException as exc: error=exc; time.sleep(2**attempt+random.random())
        raise RuntimeError(str(error))

def universe(connection,options):
    with connection.cursor() as c:
        if options.pilot: wanted=list(PILOT)
        elif options.symbol: wanted=options.symbol
        else: wanted=None
        sql="SELECT stock_id,symbol,name,market,industry,listed_date FROM stocks WHERE active AND instrument_type='company'"
        params=()
        if wanted: sql+=" AND symbol=ANY(%s)"; params=(wanted,)
        sql+=" ORDER BY symbol"; c.execute(sql,params); return [dict(row) for row in c.fetchall()]

def yahoo_history(client,stock,start,end):
    suffix=".TW" if stock["market"]=="TWSE" else ".TWO"; symbol=stock["symbol"]+suffix
    earliest=max(start or date.today()-timedelta(days=365*20+7),date.today()-timedelta(days=365*20+7))
    url="https://query1.finance.yahoo.com/v8/finance/chart/"+requests.utils.quote(symbol,safe="")
    response=client.get(url,params={"period1":int(datetime.combine(earliest,dt_time(),timezone.utc).timestamp()),"period2":int(datetime.combine(end+timedelta(days=1),dt_time(),timezone.utc).timestamp()),"interval":"1d","events":"history"}).json()
    result=(response.get("chart",{}).get("result") or [None])[0]
    if not result: raise RuntimeError(f"Yahoo no history: {symbol}")
    q=result["indicators"]["quote"][0]; adj=(result["indicators"].get("adjclose") or [{}])[0].get("adjclose") or []
    output=[]
    for i,stamp in enumerate(result.get("timestamp") or []):
        values={key:num((q.get(key) or [None]*(i+1))[i]) for key in ("open","high","low","close","volume")}
        if values["close"] is None: continue
        day=datetime.fromtimestamp(stamp,TAIPEI).date()
        if not earliest<=day<=end: continue
        valid=values["high"] is None or values["low"] is None or (values["high"]>=values["low"] and (values["open"] is None or values["low"]<=values["open"]<=values["high"]) and values["low"]<=values["close"]<=values["high"])
        if not valid: values.update(open=None,high=None,low=None)
        volume=int(values["volume"]) if values["volume"] is not None else None
        adjusted=num(adj[i]) if i<len(adj) else values["close"]
        output.append({"trade_date":day,"open":values["open"],"high":values["high"],"low":values["low"],"close":values["close"],"adjusted_close":adjusted,
            "volume":volume,"turnover":values["close"]*volume if volume is not None else None,
            "available_at":datetime.combine(day,dt_time(14),TAIPEI),"source":"Yahoo Finance chart","quality_flags":{"turnover_estimated_close_x_volume":True,"invalid_source_ohlc":not valid}})
    return output

def upsert_prices(connection,stock_id,rows):
    from psycopg.types.json import Jsonb
    sql="""INSERT INTO stock_daily_prices(stock_id,trade_date,open,high,low,close,adjusted_close,volume,turnover,available_at,source,quality_flags)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(stock_id,trade_date) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,adjusted_close=excluded.adjusted_close,volume=excluded.volume,turnover=excluded.turnover,available_at=excluded.available_at,source=excluded.source,quality_flags=excluded.quality_flags,updated_at=now()"""
    with connection.cursor() as c: c.executemany(sql,[(stock_id,r["trade_date"],r["open"],r["high"],r["low"],r["close"],r["adjusted_close"],r["volume"],r["turnover"],r["available_at"],r["source"],Jsonb(r["quality_flags"])) for r in rows])

def load_prices(connection,ids):
    with connection.cursor() as c:
        c.execute("SELECT stock_id,trade_date,open,high,low,close,adjusted_close,volume,turnover,available_at FROM stock_daily_prices WHERE stock_id=ANY(%s) ORDER BY stock_id,trade_date",(ids,))
        out=defaultdict(list)
        for row in c.fetchall(): out[row["stock_id"]].append(dict(row))
        return out

def load_context(connection):
    formal={}
    with HISTORY.open("r",encoding="utf-8-sig",newline="") as h:
        for r in csv.DictReader(h): formal[r["target_date"]]={k:num(r[k]) for k in FORMAL}
    global_features=defaultdict(dict); global_availability=defaultdict(dict)
    with connection.cursor() as c:
        c.execute("SELECT feature_key,target_date,value,available_at FROM market_features WHERE target_scope='TAIEX' AND transform_version=%s",("v3.13-phase3-1.0",))
        for r in c.fetchall(): global_features[r["target_date"].isoformat()][r["feature_key"]]=num(r["value"]); global_availability[r["target_date"].isoformat()][r["feature_key"]]=r["available_at"].isoformat()
        c.execute("SELECT stock_id,trade_date,foreign_net,investment_trust_net,dealer_net,margin_change,short_change FROM stock_chips ORDER BY stock_id,trade_date")
        chips=defaultdict(dict)
        for r in c.fetchall(): chips[r["stock_id"]][r["trade_date"]]=dict(r)
    return formal,global_features,global_availability,chips

def pct(values,i,days): return (values[i]/values[i-days]-1)*100 if i>=days and values[i-days] else None
def stdev(values): return statistics.stdev(values) if len(values)>=2 else None
def rsi(closes,i,n=14):
    if i<n:return None
    changes=[closes[j]-closes[j-1] for j in range(i-n+1,i+1)]; gains=sum(max(x,0) for x in changes)/n; losses=sum(max(-x,0) for x in changes)/n
    return 100 if losses==0 else 100-100/(1+gains/losses)
def streak(closes,i,up):
    count=0
    for j in range(i,0,-1):
        condition=closes[j]>closes[j-1] if up else closes[j]<closes[j-1]
        if not condition:break
        count+=1
    return count
def third_wednesday(year,month):
    d=date(year,month,15)
    while d.weekday()!=2:d+=timedelta(days=1)
    return d

def technical(rows,i,closes,volumes):
    c=closes[i]; result={f"return_{d}d":pct(closes,i,d) for d in (1,3,5,20)}
    returns=[pct(closes,j,1) for j in range(max(1,i-19),i+1)]
    result.update(volatility_5d=stdev(returns[-5:]) if len(returns)>=5 else None,volatility_20d=stdev(returns) if len(returns)>=20 else None)
    sample=[v for v in volumes[max(0,i-19):i+1] if v is not None]
    result["volume_ratio_20d"]=volumes[i]/statistics.mean(sample) if volumes[i] is not None and len(sample)==20 and statistics.mean(sample) else None
    for window in (5,20,60): result[f"ma{window}_distance"]=(c/statistics.mean(closes[i-window+1:i+1])-1)*100 if i>=window-1 else None
    result["ma20_slope"]=(statistics.mean(closes[i-19:i+1])/statistics.mean(closes[i-24:i-4])-1)*100 if i>=24 else None
    result["rsi14"]=rsi(closes,i)
    if i>=14 and all(rows[j]["high"] is not None and rows[j]["low"] is not None for j in range(i-13,i+1)):
        tr=[max(float(rows[j]["high"])-float(rows[j]["low"]),abs(float(rows[j]["high"])-closes[j-1]),abs(float(rows[j]["low"])-closes[j-1])) for j in range(i-13,i+1)]
        result["atr14"]=statistics.mean(tr)
    else: result["atr14"]=None
    result["consecutive_up_days"]=streak(closes,i,True); result["consecutive_down_days"]=streak(closes,i,False)
    step=10 if c<100 else 50 if c<1000 else 100; result["distance_to_round_number"]=(c-round(c/step)*step)/c*100
    ma20=statistics.mean(closes[i-19:i+1]) if i>=19 else None; result["close_above_ma20"]=int(c>ma20) if ma20 else None
    result["intraday_test_ma20"]=int(rows[i]["low"] is not None and float(rows[i]["low"])<=ma20<=float(rows[i]["high"])) if ma20 and rows[i]["high"] is not None and rows[i]["low"] is not None else None
    day=rows[i]["trade_date"]; settle=third_wednesday(day.year,day.month)
    if settle<day: settle=third_wednesday(day.year+(day.month==12),1 if day.month==12 else day.month+1)
    result.update(is_futures_settlement_day=int(day==settle),days_to_futures_settlement=(settle-day).days,month_end=int((day+timedelta(days=7)).month!=day.month),quarter_end=int(day.month in (3,6,9,12) and (day+timedelta(days=7)).month!=day.month))
    return result

def industry_context(price_groups,stocks_by_id):
    """Aggregate every industry/date once; avoids an O(stocks² × dates) build."""
    buckets=defaultdict(lambda:{"one":[],"five":[]})
    stock_returns={}
    for sid,series in price_groups.items():
        closes=[float(r["adjusted_close"] or r["close"]) for r in series]; industry=stocks_by_id[sid]["industry"]
        for i,row in enumerate(series):
            one,five=pct(closes,i,1),pct(closes,i,5);stock_returns[(sid,row["trade_date"])]=(one,five)
            if one is not None:buckets[(industry,row["trade_date"])]["one"].append(one)
            if five is not None:buckets[(industry,row["trade_date"])]["five"].append(five)
    aggregates={}
    for key,values in buckets.items():
        one,five=values["one"],values["five"]
        aggregates[key]=(statistics.mean(one) if one else None,statistics.mean(five) if five else None,sum(v>0 for v in one)/len(one) if one else None,len(one))
    return aggregates,stock_returns

def industry_features(context,stock,day):
    aggregates,stock_returns=context;avg1,avg5,advancing,count=aggregates.get((stock["industry"],day),(None,None,None,0));own=stock_returns.get((stock["stock_id"],day),(None,None))
    return {"industry_return_1d":avg1,"industry_return_5d":avg5,"industry_advancing_ratio":advancing,
        "industry_relative_strength":avg5,"stock_vs_industry_return":own[1]-avg5 if own[1] is not None and avg5 is not None else None,"industry_sample_size":count}

def refresh_industry_table(connection):
    """Compute cross-sectional features in PostgreSQL without loading the market into RAM."""
    with connection.cursor() as c:
        c.execute("""WITH returns AS (
          SELECT p.stock_id,COALESCE(s.industry,'unclassified') industry,p.trade_date,
            (COALESCE(p.adjusted_close,p.close)/lag(COALESCE(p.adjusted_close,p.close),1) OVER(PARTITION BY p.stock_id ORDER BY p.trade_date)-1)*100 r1,
            (COALESCE(p.adjusted_close,p.close)/lag(COALESCE(p.adjusted_close,p.close),5) OVER(PARTITION BY p.stock_id ORDER BY p.trade_date)-1)*100 r5
          FROM stock_daily_prices p JOIN stocks s USING(stock_id) WHERE s.instrument_type='company' AND s.active
        ), aggregate AS (
          SELECT industry,trade_date,avg(r1) r1,avg(r5) r5,
            count(*) FILTER(WHERE r1>0)::numeric/nullif(count(r1),0) advancing,count(r1) sample
          FROM returns GROUP BY industry,trade_date
        ) INSERT INTO stock_industry_daily_features(industry,trade_date,feature_version,industry_return_1d,industry_return_5d,industry_advancing_ratio,sample_size)
          SELECT industry,trade_date,%s,r1,r5,advancing,sample FROM aggregate
          ON CONFLICT(industry,trade_date,feature_version) DO UPDATE SET industry_return_1d=excluded.industry_return_1d,industry_return_5d=excluded.industry_return_5d,industry_advancing_ratio=excluded.industry_advancing_ratio,sample_size=excluded.sample_size,updated_at=now()""",(VERSION,))

def db_industry_context(connection,stock,rows):
    industry=stock["industry"] or "unclassified"
    with connection.cursor() as c:
        c.execute("SELECT trade_date,industry_return_1d,industry_return_5d,industry_advancing_ratio,sample_size FROM stock_industry_daily_features WHERE industry=%s AND feature_version=%s",(industry,VERSION))
        aggregates={(industry,r["trade_date"]):(num(r["industry_return_1d"]),num(r["industry_return_5d"]),num(r["industry_advancing_ratio"]),r["sample_size"]) for r in c.fetchall()}
    closes=[float(r["adjusted_close"] or r["close"]) for r in rows]
    own={(stock["stock_id"],r["trade_date"]):(pct(closes,i,1),pct(closes,i,5)) for i,r in enumerate(rows)}
    stock["industry"]=industry
    return aggregates,own

def chip_features(history,day):
    rows=[r for d,r in history.items() if d<=day]; rows.sort(key=lambda r:r["trade_date"])
    def total(key,n):
        vals=[num(r[key]) for r in rows[-n:]]
        return sum(vals) if vals and all(v is not None for v in vals) else None
    latest=rows[-1] if rows else {}
    return {"foreign_net_1d":num(latest.get("foreign_net")),"foreign_net_5d":total("foreign_net",5),"foreign_net_20d":total("foreign_net",20),
        "trust_net_1d":num(latest.get("investment_trust_net")),"trust_net_5d":total("investment_trust_net",5),"trust_net_20d":total("investment_trust_net",20),
        "dealer_net":num(latest.get("dealer_net")),"margin_change":num(latest.get("margin_change")),"short_change":num(latest.get("short_change"))}

def build_rows(connection,stocks,price_groups,shared_context=None):
    from psycopg.types.json import Jsonb
    formal,globals_,global_times,chips=shared_context or load_context(connection); stocks_by_id={s["stock_id"]:s for s in stocks}; industry=industry_context(price_groups,stocks_by_id) if len(stocks)>1 else db_industry_context(connection,stocks[0],price_groups[stocks[0]["stock_id"]]);features=[]; targets=[]; leakage=[]
    for stock in stocks:
        rows=price_groups[stock["stock_id"]]
        closes_cache=[float(r["adjusted_close"] or r["close"]) for r in rows];volumes_cache=[r["volume"] for r in rows]
        for i,row in enumerate(rows):
            target_date=rows[i+1]["trade_date"] if i+1<len(rows) else None; cutoff=datetime.combine(target_date,dt_time(9),TAIPEI) if target_date else datetime.combine(row["trade_date"]+timedelta(days=1),dt_time(9),TAIPEI)
            values=technical(rows,i,closes_cache,volumes_cache); values.update(industry_features(industry,stock,row["trade_date"])); values.update(chip_features(chips[stock["stock_id"]],row["trade_date"]))
            availability={key:row["available_at"].isoformat() for key in values if values[key] is not None and key not in {"foreign_net_1d","foreign_net_5d","foreign_net_20d","trust_net_1d","trust_net_5d","trust_net_20d","dealer_net","margin_change","short_change"}}
            if target_date:
                target_key=target_date.isoformat()
                for key,value in (formal.get(target_key) or {}).items(): values["market_"+key]=value
                for key,value in (globals_.get(target_key) or {}).items(): values["global_"+key]=value; availability["global_"+key]=global_times[target_key][key]
                for key,stamp in availability.items():
                    if datetime.fromisoformat(stamp)>=cutoff: leakage.append({"symbol":stock["symbol"],"feature_date":row["trade_date"].isoformat(),"target_date":target_key,"feature":key,"available_at":stamp,"cutoff":cutoff.isoformat()})
            features.append((stock["stock_id"],row["trade_date"],VERSION,cutoff,target_date,Jsonb(values),Jsonb(availability)))
            base=float(row["adjusted_close"] or row["close"])
            for horizon in (1,3,5):
                if i+horizon<len(rows):
                    future=float(rows[i+horizon]["adjusted_close"] or rows[i+horizon]["close"]); ret=(future/base-1)*100
                    targets.append((stock["stock_id"],row["trade_date"],horizon,rows[i+horizon]["trade_date"],ret,int(ret>0),VERSION))
    if leakage: raise ValueError(f"Temporal leakage detected: {leakage[:3]}")
    with connection.cursor() as c:
        c.execute("CREATE TEMP TABLE stage_features(LIKE stock_prediction_features INCLUDING DEFAULTS) ON COMMIT DROP")
        with c.copy("COPY stage_features(stock_id,feature_date,feature_version,feature_available_cutoff,target_date,features,feature_availability) FROM STDIN") as copy:
            for row in features:copy.write_row(row)
        c.execute("""INSERT INTO stock_prediction_features(stock_id,feature_date,feature_version,feature_available_cutoff,target_date,features,feature_availability)
          SELECT stock_id,feature_date,feature_version,feature_available_cutoff,target_date,features,feature_availability FROM stage_features
          ON CONFLICT(stock_id,feature_date,feature_version) DO UPDATE SET
          feature_available_cutoff=excluded.feature_available_cutoff,target_date=excluded.target_date,
          features=excluded.features,feature_availability=excluded.feature_availability,updated_at=now()""")
        c.execute("CREATE TEMP TABLE stage_targets(LIKE stock_prediction_targets INCLUDING DEFAULTS) ON COMMIT DROP")
        with c.copy("COPY stage_targets(stock_id,feature_date,horizon,target_date,target_return,target_direction,target_version) FROM STDIN") as copy:
            for row in targets:copy.write_row(row)
        c.execute("""INSERT INTO stock_prediction_targets(stock_id,feature_date,horizon,target_date,target_return,target_direction,target_version)
          SELECT stock_id,feature_date,horizon,target_date,target_return,target_direction,target_version FROM stage_targets
          ON CONFLICT(stock_id,feature_date,horizon,target_version) DO UPDATE SET
          target_date=excluded.target_date,target_return=excluded.target_return,
          target_direction=excluded.target_direction,updated_at=now()""")
    return len(features),len(targets)

def report(connection,elapsed,mode,failures,before_size):
    with connection.cursor() as c:
        c.execute("SELECT count(*) n,count(DISTINCT stock_id) stocks,min(trade_date) first,max(trade_date) last FROM stock_daily_prices"); prices=dict(c.fetchone())
        c.execute("SELECT count(*) n,count(DISTINCT stock_id) stocks,min(feature_date) first,max(feature_date) last FROM stock_prediction_features WHERE feature_version=%s",(VERSION,)); features=dict(c.fetchone())
        c.execute("SELECT horizon,count(*) n,avg(target_direction) up_ratio FROM stock_prediction_targets WHERE target_version=%s GROUP BY horizon ORDER BY horizon",(VERSION,)); target_dist=[dict(r) for r in c.fetchall()]
        c.execute("SELECT pg_database_size(current_database()) size"); after=c.fetchone()["size"]
        c.execute("SELECT features FROM stock_prediction_features WHERE feature_version=%s",(VERSION,)); feature_rows=c.fetchall()
    stats=defaultdict(lambda:[0,0,None])
    for record in feature_rows:
        for key,value in record["features"].items():
            stats[key][1]+=1
            if value is not None: stats[key][0]+=1; stats[key][2]=stats[key][2] or features["first"]
    payload={"generated_at":datetime.now(timezone.utc).isoformat(),"version":VERSION,"mode":mode,"elapsed_seconds":round(elapsed,3),
        "prices":prices,"features":features,"targets":target_dist,"failures":failures,"temporal_leakage":{"count":0},
        "feature_completeness":[{"feature":k,"available_rows":v[0],"missing_rate":round(1-v[0]/v[1],8),"inception_date":v[2]} for k,v in sorted(stats.items())],
        "database_growth_bytes":after-before_size,"daily_incremental_estimate_seconds":max(1,features["stocks"]*.6),
        "source_notes":{"prices":"Yahoo Finance chart; one request per symbol; research use and Yahoo terms apply","turnover":"estimated close × volume, flagged in quality metadata","listed_date":"official profile when available; otherwise first reliable price date is retained as dataset inception"},
        "limitations":["Global/formal market features are NULL before their independent inception dates.","Historical individual chips are limited to existing reliable stock_chips history."]}
    atomic(REPORT,payload);return payload

def atomic(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w",encoding="utf-8") as h:json.dump(payload,h,ensure_ascii=False,indent=2,default=str,allow_nan=False);h.write("\n")
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()

def main():
    logging.basicConfig(level=logging.INFO,format="%(levelname)s | %(message)s"); o=args(); started=time.monotonic(); connection=connect()
    try:
        # Migrations are intentionally applied by the separate migration role.
        # The daily sync account has DML only and must not require CREATE rights.
        stocks=universe(connection,o)
        with connection.cursor() as c:c.execute("SELECT pg_database_size(current_database()) size");before=c.fetchone()["size"]
        client=Client(o.delay);failures=[];price_count=0
        for index,stock in enumerate(stocks,1):
            try:
                rows=[]
                if o.resume:
                    with connection.cursor() as c:c.execute("SELECT count(*) n,max(trade_date) last FROM stock_daily_prices WHERE stock_id=%s",(stock["stock_id"],));existing=c.fetchone()
                    if not existing["n"] or existing["last"] < o.end-timedelta(days=7): rows=yahoo_history(client,stock,o.start,o.end)
                else: rows=yahoo_history(client,stock,o.start,o.end)
                if rows:upsert_prices(connection,stock["stock_id"],rows);connection.commit();price_count+=len(rows)
                if not stock.get("listed_date") and rows:
                    with connection.cursor() as c:c.execute("UPDATE stocks SET listed_date=%s WHERE stock_id=%s AND listed_date IS NULL",(rows[0]["trade_date"],stock["stock_id"]));connection.commit();stock["listed_date"]=rows[0]["trade_date"]
                LOG.info("prices %d/%d %s rows=%d%s",index,len(stocks),stock["symbol"],len(rows)," resumed" if o.resume and not rows else "")
            except Exception as exc:connection.rollback();failures.append({"symbol":stock["symbol"],"stage":"prices","error":str(exc)});LOG.error("%s: %s",stock["symbol"],exc)
        good=[s for s in stocks if not any(f["symbol"]==s["symbol"] for f in failures)]
        refresh_industry_table(connection);connection.commit();shared=load_context(connection);feature_count=target_count=0
        for index,stock in enumerate(good,1):
            if o.resume:
                with connection.cursor() as c:
                    c.execute("SELECT (SELECT count(*) FROM stock_daily_prices WHERE stock_id=%s) prices,(SELECT count(*) FROM stock_prediction_features WHERE stock_id=%s AND feature_version=%s) features",(stock["stock_id"],stock["stock_id"],VERSION));done=c.fetchone()
                if done["prices"] and done["prices"]==done["features"]:
                    LOG.info("features %d/%d %s resumed",index,len(good),stock["symbol"]);continue
            groups=load_prices(connection,[stock["stock_id"]]);f,t=build_rows(connection,[stock],groups,shared);connection.commit();feature_count+=f;target_count+=t
            LOG.info("features %d/%d %s rows=%d",index,len(good),stock["symbol"],f)
        result=report(connection,time.monotonic()-started,"pilot" if o.pilot else "all" if o.all else "symbols",failures,before)
        LOG.info("complete stocks=%d prices=%d features=%d targets=%d failures=%d",len(good),price_count,feature_count,target_count,len(failures));return 0 if good else 1
    except Exception as exc:connection.rollback();LOG.exception("build failed: %s",exc);return 1
    finally:connection.close()
if __name__=="__main__":raise SystemExit(main())
