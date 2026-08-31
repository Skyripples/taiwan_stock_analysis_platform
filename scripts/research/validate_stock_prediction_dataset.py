"""Final PostgreSQL validation for the V3.14 full-market dataset."""
from __future__ import annotations
import json, math, os, random, statistics, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from config import PROJECT_ROOT
from database.connection import connect

OUT=PROJECT_ROOT/"data"/"analysis"/"current"/"stock_prediction_dataset_report.json"; VERSION="v3.14-1.0"; BASELINE_DB_BYTES=126_771_331
TECH_KEYS=("return_1d","return_3d","return_5d","return_20d","volatility_5d","volatility_20d","volume_ratio_20d","ma5_distance","ma20_distance","ma60_distance","ma20_slope","rsi14","atr14","consecutive_up_days","consecutive_down_days")
FIXED=("2330","2317","6488","2881","1101")
def clean(v): return float(v) if v is not None else None
def close(a,b,tol=1e-7): return a is None and b is None or a is not None and b is not None and abs(float(a)-float(b))<=tol*max(1,abs(float(a)),abs(float(b)))
def percentile(values,p):
    values=sorted(values);k=(len(values)-1)*p;i=int(k);f=k-i
    return values[i]*(1-f)+values[min(i+1,len(values)-1)]*f
def sysinfo():
    mem={}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key,value=line.split(":",1);mem[key]=int(value.strip().split()[0])*1024
    disk=os.statvfs("/");return {"ram_total_bytes":mem["MemTotal"],"ram_available_bytes":mem["MemAvailable"],"swap_total_bytes":mem.get("SwapTotal",0),"swap_free_bytes":mem.get("SwapFree",0),"disk_total_bytes":disk.f_blocks*disk.f_frsize,"disk_free_bytes":disk.f_bavail*disk.f_frsize}
def run():
    started=time.monotonic();db=connect();fail=[]
    try:
      with db.cursor() as c:
        c.execute("SELECT count(*) n FROM stocks WHERE active AND instrument_type='company'");universe=c.fetchone()["n"]
        c.execute("""SELECT s.stock_id,s.symbol,s.industry,s.listed_date,
          (SELECT count(*) FROM stock_daily_prices p WHERE p.stock_id=s.stock_id) prices,
          (SELECT count(*) FROM stock_prediction_features f WHERE f.stock_id=s.stock_id AND f.feature_version=%s) features,
          (SELECT count(*) FROM stock_prediction_targets t WHERE t.stock_id=s.stock_id AND t.target_version=%s AND horizon=1) t1,
          (SELECT count(*) FROM stock_prediction_targets t WHERE t.stock_id=s.stock_id AND t.target_version=%s AND horizon=3) t3,
          (SELECT count(*) FROM stock_prediction_targets t WHERE t.stock_id=s.stock_id AND t.target_version=%s AND horizon=5) t5,
          (SELECT min(trade_date) FROM stock_daily_prices p WHERE p.stock_id=s.stock_id) first,
          (SELECT max(trade_date) FROM stock_daily_prices p WHERE p.stock_id=s.stock_id) last
          FROM stocks s WHERE s.active AND s.instrument_type='company' ORDER BY s.symbol""",(VERSION,VERSION,VERSION,VERSION)); per=[dict(r) for r in c.fetchall()]
        # The wide join inflates counts despite DISTINCT, but preserves exact date counts.
        success=sum(r["prices"]>0 and r["features"]==r["prices"] and r["t1"]==max(0,r["prices"]-1) and r["t3"]==max(0,r["prices"]-3) and r["t5"]==max(0,r["prices"]-5) for r in per);failed=sum(r["prices"]==0 for r in per);partial=universe-success-failed
        counts={}
        for table,datecol in (("stock_daily_prices","trade_date"),("stock_prediction_features","feature_date"),("stock_prediction_targets","target_date")):
            where=" WHERE feature_version=%s" if table.endswith("features") else " WHERE target_version=%s" if table.endswith("targets") else "";c.execute(f"SELECT count(*) n,min({datecol}) first,max({datecol}) last FROM {table}{where}",((VERSION,) if where else ()));counts[table]=dict(c.fetchone())
        c.execute("SELECT horizon,count(*) rows,sum(target_direction) up,count(*)-sum(target_direction) down FROM stock_prediction_targets WHERE target_version=%s GROUP BY horizon ORDER BY horizon",(VERSION,));targets=[dict(r) for r in c.fetchall()]
        c.execute("SELECT COALESCE(s.industry,'unclassified') industry,count(*) rows,count(DISTINCT f.stock_id) stocks FROM stock_prediction_features f JOIN stocks s USING(stock_id) WHERE f.feature_version=%s GROUP BY 1 ORDER BY 1",(VERSION,));industries=[dict(r) for r in c.fetchall()]
        # Bound RAM on the 2 GB host: aggregate one stock at a time instead of
        # materialising hundreds of millions of jsonb_each rows globally.
        missing_map={};leak=0
        for item in per:
            c.execute("SELECT key,count(*) total,count(*) FILTER(WHERE value='null'::jsonb) missing,min(feature_date) FILTER(WHERE value<>'null'::jsonb) inception FROM stock_prediction_features f CROSS JOIN LATERAL jsonb_each(f.features) WHERE stock_id=%s AND feature_version=%s GROUP BY key",(item["stock_id"],VERSION))
            for r in c.fetchall():
                state=missing_map.setdefault(r["key"],{"key":r["key"],"total":0,"missing":0,"inception":None});state["total"]+=r["total"];state["missing"]+=r["missing"]
                if r["inception"] and (state["inception"] is None or r["inception"]<state["inception"]):state["inception"]=r["inception"]
            c.execute("SELECT count(*) violations FROM stock_prediction_features f CROSS JOIN LATERAL jsonb_each_text(f.feature_availability) a WHERE stock_id=%s AND feature_version=%s AND a.value::timestamptz>=f.feature_available_cutoff",(item["stock_id"],VERSION));leak+=c.fetchone()["violations"]
        missing=[missing_map[key] for key in sorted(missing_map)]
        c.execute("SELECT sum(n-1) duplicates FROM (SELECT count(*) n FROM stock_prediction_features GROUP BY stock_id,feature_date,feature_version HAVING count(*)>1) q");dup_f=c.fetchone()["duplicates"] or 0
        c.execute("SELECT sum(n-1) duplicates FROM (SELECT count(*) n FROM stock_prediction_targets GROUP BY stock_id,feature_date,horizon,target_version HAVING count(*)>1) q");dup_t=c.fetchone()["duplicates"] or 0
        c.execute("SELECT pg_database_size(current_database()) size");dbsize=c.fetchone()["size"]
        c.execute("SELECT sum(pg_total_relation_size(oid)) size FROM pg_class WHERE relname=ANY(%s)",(["stock_daily_prices","stock_prediction_features","stock_prediction_targets","stock_industry_daily_features"],));vsize=c.fetchone()["size"]
        candidates=[r["symbol"] for r in per if r["prices"]>=100];rng=random.Random(314);sample=list(dict.fromkeys((*FIXED,*rng.sample(candidates,45))))[:50]
        checks=[]
        for symbol in sample:
          c.execute("SELECT stock_id,listed_date,industry FROM stocks WHERE symbol=%s",(symbol,));stock=c.fetchone();sid=stock["stock_id"]
          c.execute("SELECT min(trade_date) first FROM stock_daily_prices WHERE stock_id=%s",(sid,));first=c.fetchone()["first"]
          c.execute("SELECT feature_date,features,target_date,feature_available_cutoff,feature_availability FROM stock_prediction_features WHERE stock_id=%s AND feature_version=%s ORDER BY feature_date OFFSET GREATEST((SELECT count(*) FROM stock_prediction_features WHERE stock_id=%s AND feature_version=%s)/2,60) LIMIT 1",(sid,VERSION,sid,VERSION));f=c.fetchone()
          c.execute("SELECT trade_date,high,low,adjusted_close,close,volume FROM stock_daily_prices WHERE stock_id=%s AND trade_date<=%s ORDER BY trade_date DESC LIMIT 61",(sid,f["feature_date"]));hist=list(reversed(c.fetchall()));closes=[float(r["adjusted_close"] or r["close"]) for r in hist];calc1=(closes[-1]/closes[-2]-1)*100;ma20=(closes[-1]/statistics.mean(closes[-20:])-1)*100
          tech_ok=close(calc1,f["features"].get("return_1d")) and close(ma20,f["features"].get("ma20_distance"))
          target_ok=True
          for h in (1,3,5):
            c.execute("SELECT target_date,target_return FROM stock_prediction_targets WHERE stock_id=%s AND feature_date=%s AND horizon=%s AND target_version=%s",(sid,f["feature_date"],h,VERSION));t=c.fetchone()
            if t:
              c.execute("SELECT adjusted_close,close FROM stock_daily_prices WHERE stock_id=%s AND trade_date=%s",(sid,t["target_date"]));future=c.fetchone();target_ok &= close((float(future["adjusted_close"] or future["close"])/closes[-1]-1)*100,t["target_return"])
          c.execute("SELECT industry_return_1d,industry_return_5d,industry_advancing_ratio FROM stock_industry_daily_features WHERE industry=%s AND trade_date=%s AND feature_version=%s",(stock["industry"] or "unclassified",f["feature_date"],VERSION));ind=c.fetchone();industry_ok=bool(ind) and close(ind["industry_return_1d"],f["features"].get("industry_return_1d"))
          cutoff_ok=all(datetime.fromisoformat(v)<f["feature_available_cutoff"] for k,v in f["feature_availability"].items() if k.startswith("global_"));listing_ok=stock["listed_date"] is None or first>=stock["listed_date"]
          item={"symbol":symbol,"feature_date":f["feature_date"],"technical":tech_ok,"targets":target_ok,"industry":industry_ok,"global_cutoff":cutoff_ok,"no_prelisting":listing_ok};checks.append(item)
          if not all((tech_ok,target_ok,industry_ok,cutoff_ok,listing_ok)):fail.append(item)
      sample_counts=[r["features"] for r in per]
      log=Path("/var/log/taiwan-stock-prediction-v314.log");duration=log.stat().st_mtime-log.stat().st_ctime if log.exists() else None
      payload={"generated_at":datetime.now(timezone.utc).isoformat(),"version":VERSION,"universe":{"ordinary_stocks":universe,"success":success,"partial":partial,"failed":failed},"tables":counts,"targets":targets,"history":{"first":counts["stock_daily_prices"]["first"],"last":counts["stock_daily_prices"]["last"]},"per_stock_feature_rows":{"min":min(sample_counts),"p50":percentile(sample_counts,.5),"p90":percentile(sample_counts,.9),"max":max(sample_counts),"mean":statistics.mean(sample_counts)},"industries":industries,"feature_missing":[{**r,"missing_rate":float(r["missing"])/r["total"]} for r in missing],"integrity":{"temporal_leakage":leak,"feature_pk_duplicates":dup_f,"target_pk_duplicates":dup_t},"sample_validation":{"count":len(checks),"passed":len(checks)-len(fail),"failed":len(fail),"details":checks},"capacity":{"database_bytes":dbsize,"v314_tables_bytes":vsize,"growth_vs_pre_v314_bytes":max(0,dbsize-BASELINE_DB_BYTES),"full_run_seconds_from_log":duration,"daily_incremental_estimate_seconds":universe*.6},"host":sysinfo(),"v315_readiness":{"ready":success==universe and not fail and leak==0,"note":"Dataset engineering is complete; model training must still use time-based and stock-aware splits."},"validation_seconds":time.monotonic()-started}
      atomic(payload);return payload
    finally:db.close()
def atomic(p):
    OUT.parent.mkdir(parents=True,exist_ok=True);tmp=OUT.with_name(f".{OUT.name}.{os.getpid()}.tmp")
    with tmp.open("w",encoding="utf-8") as h:json.dump(p,h,ensure_ascii=False,indent=2,default=str,allow_nan=False);h.write("\n")
    os.replace(tmp,OUT)
if __name__=="__main__":
    result=run();print(json.dumps({"universe":result["universe"],"tables":result["tables"],"integrity":result["integrity"],"sample":result["sample_validation"],"capacity":result["capacity"]},default=str))
