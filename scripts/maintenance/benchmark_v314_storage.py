"""Comparable read benchmarks for PostgreSQL and the local DuckDB/Parquet lake."""
from __future__ import annotations
import argparse,json,os,statistics,time
from datetime import datetime,timezone
from pathlib import Path
from config import PROJECT_ROOT

def percentile(values,p):
 values=sorted(values);index=(len(values)-1)*p;lower=int(index);fraction=index-lower
 return values[lower]*(1-fraction)+values[min(lower+1,len(values)-1)]*fraction
def rss():
 try:
  import psutil
  return psutil.Process().memory_info().rss
 except ImportError:return None
def measure(callback,repeats=8):
 values=[];peak=rss() or 0;rows=None
 for _ in range(repeats):
  started=time.perf_counter();rows=callback();values.append((time.perf_counter()-started)*1000);peak=max(peak,rss() or 0)
 return {"cold_ms":round(values[0],3),"warm_p50_ms":round(statistics.median(values[1:]),3),"warm_p95_ms":round(percentile(values[1:],.95),3),"rows":rows,"peak_rss_bytes":peak}
def parquet(lake):
 from maintenance.query_v314_data_lake import connect
 db=connect(lake)
 queries={
  "single_stock":lambda:len(db.execute("SELECT feature_date,return_1d,rsi14 FROM features WHERE symbol='2330'").fetchall()),
  "latest_cross_section":lambda:len(db.execute("SELECT symbol,return_1d,rsi14 FROM features WHERE feature_date=(SELECT max(feature_date) FROM features)").fetchall()),
  "ml_dataframe_100k":lambda:len(db.execute("SELECT f.symbol,f.feature_date,f.return_1d,f.rsi14,t.target_return,t.target_direction FROM features f JOIN targets t USING(symbol,feature_date) WHERE t.horizon=1 ORDER BY f.feature_date DESC LIMIT 100000").fetchall()),
 }
 return {name:measure(query) for name,query in queries.items()}
def postgres():
 from database.connection import connect
 db=connect()
 def execute(sql):
  with db.cursor() as cursor:cursor.execute(sql);return len(cursor.fetchall())
 queries={
  "single_stock":lambda:execute("SELECT f.feature_date,f.features->>'return_1d',f.features->>'rsi14' FROM stock_prediction_features f JOIN stocks s USING(stock_id) WHERE s.symbol='2330' AND f.feature_version='v3.14-1.0'"),
  "latest_cross_section":lambda:execute("SELECT s.symbol,f.features->>'return_1d',f.features->>'rsi14' FROM stock_prediction_features f JOIN stocks s USING(stock_id) WHERE f.feature_version='v3.14-1.0' AND f.feature_date=(SELECT max(feature_date) FROM stock_prediction_features WHERE feature_version='v3.14-1.0')"),
  "ml_dataframe_100k":lambda:execute("SELECT s.symbol,f.feature_date,f.features->>'return_1d',f.features->>'rsi14',t.target_return,t.target_direction FROM stock_prediction_features f JOIN stocks s USING(stock_id) JOIN stock_prediction_targets t USING(stock_id,feature_date) WHERE f.feature_version='v3.14-1.0' AND t.target_version='v3.14-1.0' AND t.horizon=1 ORDER BY f.feature_date DESC LIMIT 100000"),
 }
 try:return {name:measure(query) for name,query in queries.items()}
 finally:db.close()
if __name__=="__main__":
 parser=argparse.ArgumentParser();parser.add_argument("--backend",choices=("parquet","postgres"),required=True);parser.add_argument("--lake",type=Path,default=PROJECT_ROOT/"data_lake");parser.add_argument("--output",type=Path);args=parser.parse_args()
 started=time.perf_counter();result={"generated_at":datetime.now(timezone.utc).isoformat(),"backend":args.backend,"queries":parquet(args.lake) if args.backend=="parquet" else postgres(),"seconds":round(time.perf_counter()-started,3)}
 text=json.dumps(result,indent=2);print(text)
 if args.output:args.output.write_text(text+"\n",encoding="utf-8")
