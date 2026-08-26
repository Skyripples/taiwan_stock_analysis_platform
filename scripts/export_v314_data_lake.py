"""Stream V3.14 PostgreSQL research tables into partitioned Parquet + ZSTD."""
from __future__ import annotations
import argparse, json, logging, os, shutil, time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from config import PROJECT_ROOT
from database.connection import connect

LOG=logging.getLogger("v314_export"); VERSION="v3.14-1.0"
FORMAL=("taiex_close","taiex_change_percent","tpex_close","turnover","advancing","declining","foreign_cash_flow","foreign_futures_position","night_futures_change","tsm_adr_change_percent","sox_change_percent","sp500_change_percent","nasdaq_change_percent","vix_change_percent","kospi_change_percent")
DEFAULT=PROJECT_ROOT/"data_lake"
TABLES=("stock_daily_prices","stock_prediction_features","stock_prediction_targets","stock_industry_daily_features","global_market")

def args():
 p=argparse.ArgumentParser();p.add_argument("--output",type=Path,default=DEFAULT);p.add_argument("--batch-size",type=int,default=10000);p.add_argument("--overwrite",action="store_true");p.add_argument("--global-only",action="store_true");p.add_argument("--features-only",action="store_true");p.add_argument("--targets-only",action="store_true");return p.parse_args()
def scalar(v):
 if isinstance(v,(date,datetime)):return v
 if hasattr(v,"as_tuple"):return float(v)
 return v
def write_query(db,root,name,query,params,partition_cols,batch_size,flatten=None,arrow_types=None):
 import pyarrow as pa, pyarrow.parquet as pq
 target=root/name
 if target.exists():shutil.rmtree(target)
 target.mkdir(parents=True);rows=files=bytes_=0;started=time.monotonic();writer=None;current=None;path=None
 with db.cursor(name=f"export_{name}") as c:
  c.itersize=batch_size;c.execute(query,params);columns=[d.name for d in c.description]
  while True:
   batch=c.fetchmany(batch_size)
   if not batch:break
   records=[]
   for source in batch:
    record={k:scalar(source[k]) for k in columns}
    if flatten:record=flatten(record)
    records.append(record)
   # query ordering guarantees a batch normally contains few partition changes;
   # split exactly so each partition has one reasonably large file.
   groups=[]
   for record in records:
    key=tuple(record[col] for col in partition_cols)
    if not groups or groups[-1][0]!=key:groups.append((key,[]))
    groups[-1][1].append(record)
   for key,items in groups:
    if key!=current:
     if writer:writer.close();files+=1;bytes_+=path.stat().st_size
     directory=target
     for col,value in zip(partition_cols,key):directory=directory/f"{col}={value}"
     directory.mkdir(parents=True,exist_ok=True);path=directory/"part-00000.parquet";writer=None;current=key
    table=pa.Table.from_pylist(items)
    for column,type_name in (arrow_types or {}).items():
     if column in table.column_names:
      index=table.column_names.index(column);table=table.set_column(index,column,pa.array([item[column] for item in items],type=getattr(pa,type_name)()))
    if writer is None:
     dictionary_columns=[name for name in ("symbol","market","feature_version","target_version","industry","category","canonical_symbol","feature_key","source") if name in table.schema.names]
     writer=pq.ParquetWriter(path,table.schema,compression="zstd",compression_level=6,use_dictionary=dictionary_columns)
    elif table.schema != writer.schema:table=table.cast(writer.schema,safe=False)
    writer.write_table(table,row_group_size=100000);rows+=len(items)
 if writer:writer.close();files+=1;bytes_+=path.stat().st_size
 return {"rows":rows,"files":files,"bytes":bytes_,"seconds":round(time.monotonic()-started,3)}
def feature_keys(db):
 with db.cursor() as c:
  c.execute("SELECT features FROM stock_prediction_features WHERE feature_version=%s ORDER BY feature_date DESC LIMIT 1",(VERSION,));keys=set(c.fetchone()["features"])
  keys.update("market_"+name for name in FORMAL)
  c.execute("SELECT DISTINCT feature_key FROM market_features WHERE target_scope='TAIEX'")
  keys.update("global_"+row["feature_key"] for row in c.fetchall())
  return sorted(keys)
def feature_flat(keys):
 def flatten(r):
  values=r.pop("features") or {};availability=r.pop("feature_availability") or {}
  for key in keys:r[key]=scalar(values.get(key))
  r["availability_json"]=json.dumps(availability,separators=(",",":"),sort_keys=True)
  return r
 return flatten
def feature_types(keys):
 integer={"close_above_ma20","consecutive_down_days","consecutive_up_days","days_to_futures_settlement","industry_sample_size","intraday_test_ma20","is_futures_settlement_day","month_end","quarter_end"}
 return {key:("int64" if key in integer else "float64") for key in keys}
def stringify_complex(r):
 """Keep nested metadata lossless without Arrow's empty-struct limitation."""
 for key,value in tuple(r.items()):
  if isinstance(value,(dict,list)):r[key]=json.dumps(value,separators=(",",":"),sort_keys=True)
 return r
def stock_partitions(db):
 with db.cursor() as c:
  c.execute("SELECT DISTINCT s.market,extract(year from p.trade_date)::int partition_year FROM stock_daily_prices p JOIN stocks s USING(stock_id) ORDER BY 1,2")
  return [(r["market"],r["partition_year"]) for r in c.fetchall()]
def stock_years(db):return sorted({year for _,year in stock_partitions(db)})
def merge_stats(items):
 return {"rows":sum(x["rows"] for x in items),"files":sum(x["files"] for x in items),"bytes":sum(x["bytes"] for x in items),"seconds":round(sum(x["seconds"] for x in items),3)}
def parquet_stats(path):
 import pyarrow.parquet as pq
 files=list(path.rglob("*.parquet"))
 return {"rows":sum(pq.ParquetFile(f).metadata.num_rows for f in files),"files":len(files),"bytes":sum(f.stat().st_size for f in files),"seconds":0.0}
def export_stock_partitions(db,root,name,select_sql,date_field,version_field,version,batch,flatten=None):
 target=root/name
 if target.exists():shutil.rmtree(target)
 stats=[]
 for market,year in stock_partitions(db):
  query=f"{select_sql} WHERE s.market=%s AND extract(year from {date_field})=%s" + (f" AND {version_field}=%s" if version_field else "")
  params=(market,year,version) if version_field else (market,year)
  stats.append(write_query(db,target/f"market={market}",f"partition_year={year}",query,params,(),batch,flatten))
 return merge_stats(stats)
def export_stock_years(db,root,name,select_sql,date_field,version_field,version,batch,flatten=None,arrow_types=None):
 target=root/name
 if target.exists():shutil.rmtree(target)
 stats=[]
 for year in stock_years(db):
  query=f"{select_sql} WHERE {date_field}>=%s AND {date_field}<%s"+(f" AND {version_field}=%s" if version_field else "")
  params=(date(year,1,1),date(year+1,1,1),version) if version_field else (date(year,1,1),date(year+1,1,1))
  stats.append(write_query(db,target,f"partition_year={year}",query,params,(),batch,flatten,arrow_types))
 return merge_stats(stats)
def export(root,batch,global_only=False,features_only=False,targets_only=False):
 db=connect();root.mkdir(parents=True,exist_ok=True);result={};keys=feature_keys(db)
 try:
  if global_only:
   for name in TABLES[:4]:result[name]=parquet_stats(root/name)
  elif features_only:
   result["stock_daily_prices"]=parquet_stats(root/"stock_daily_prices")
   result["stock_prediction_features"]=export_stock_years(db,root,"stock_prediction_features","SELECT s.market,extract(year from f.feature_date)::int partition_year,s.symbol,f.feature_date,f.target_date,f.feature_version,f.feature_available_cutoff,f.features,f.feature_availability FROM stock_prediction_features f JOIN stocks s USING(stock_id)","f.feature_date","f.feature_version",VERSION,batch,feature_flat(keys),feature_types(keys))
   result["stock_prediction_targets"]=parquet_stats(root/"stock_prediction_targets")
   result["stock_industry_daily_features"]=parquet_stats(root/"stock_industry_daily_features")
  elif targets_only:
   result["stock_daily_prices"]=parquet_stats(root/"stock_daily_prices")
   result["stock_prediction_features"]=parquet_stats(root/"stock_prediction_features")
   result["stock_prediction_targets"]=export_stock_years(db,root,"stock_prediction_targets","SELECT s.market,extract(year from t.feature_date)::int partition_year,s.symbol,t.feature_date,t.horizon,t.target_date,t.target_return,t.target_direction,t.target_version FROM stock_prediction_targets t JOIN stocks s USING(stock_id)","t.feature_date","t.target_version",VERSION,batch)
   result["stock_industry_daily_features"]=parquet_stats(root/"stock_industry_daily_features")
  else:
   result["stock_daily_prices"]=export_stock_partitions(db,root,"stock_daily_prices","SELECT s.market,extract(year from p.trade_date)::int partition_year,s.symbol,p.trade_date,p.open,p.high,p.low,p.close,p.adjusted_close,p.volume,p.turnover,p.available_at,p.source FROM stock_daily_prices p JOIN stocks s USING(stock_id)","p.trade_date",None,None,batch)
   result["stock_prediction_features"]=export_stock_years(db,root,"stock_prediction_features","SELECT s.market,extract(year from f.feature_date)::int partition_year,s.symbol,f.feature_date,f.target_date,f.feature_version,f.feature_available_cutoff,f.features,f.feature_availability FROM stock_prediction_features f JOIN stocks s USING(stock_id)","f.feature_date","f.feature_version",VERSION,batch,feature_flat(keys),feature_types(keys))
   result["stock_prediction_targets"]=export_stock_years(db,root,"stock_prediction_targets","SELECT s.market,extract(year from t.feature_date)::int partition_year,s.symbol,t.feature_date,t.horizon,t.target_date,t.target_return,t.target_direction,t.target_version FROM stock_prediction_targets t JOIN stocks s USING(stock_id)","t.feature_date","t.target_version",VERSION,batch)
   result["stock_industry_daily_features"]=write_query(db,root,"stock_industry_daily_features","SELECT extract(year from trade_date)::int partition_year,* FROM stock_industry_daily_features WHERE feature_version=%s ORDER BY partition_year,industry,trade_date",(VERSION,),("partition_year",),batch)
  result["stock_dimension"]=write_query(db,root/"dimensions","stocks","SELECT stock_id,symbol,name,market,industry,instrument_type,active,listed_date,source_updated_at FROM stocks ORDER BY symbol",(),(),batch,stringify_complex)
  result["global_daily_prices"]=write_query(db,root/"global_market","daily_prices","SELECT mi.category,extract(year from p.trade_date)::int partition_year,mi.canonical_symbol,p.* FROM market_daily_prices p JOIN market_instruments mi USING(instrument_id) ORDER BY mi.category,partition_year,mi.canonical_symbol,p.trade_date",(),("category","partition_year"),batch,stringify_complex)
  result["global_intraday_prices"]=write_query(db,root/"global_market","intraday_prices","SELECT mi.category,extract(year from p.timestamp_utc)::int partition_year,mi.canonical_symbol,p.* FROM market_intraday_prices p JOIN market_instruments mi USING(instrument_id) ORDER BY mi.category,partition_year,mi.canonical_symbol,p.timestamp_utc",(),("category","partition_year"),batch,stringify_complex)
  result["global_macro_observations"]=write_query(db,root/"global_market","macro_observations","SELECT ms.series_key,extract(year from o.observation_date)::int partition_year,o.* FROM macro_observations o JOIN macro_series ms USING(macro_series_id) ORDER BY ms.series_key,partition_year,o.observation_date",(),("series_key","partition_year"),batch,stringify_complex)
  result["global_market_features"]=write_query(db,root/"global_market","market_features","SELECT extract(year from target_date)::int partition_year,* FROM market_features ORDER BY partition_year,feature_key,target_date",(),("partition_year",),batch,stringify_complex,{"source_instrument_id":"int64"})
 finally:db.close()
 manifest={"generated_at":datetime.now(timezone.utc).isoformat(),"format":"Parquet","compression":"ZSTD level 6","feature_version":VERSION,"feature_columns":keys,"datasets":result,"total_bytes":sum(v["bytes"] for v in result.values()),"total_files":sum(v["files"] for v in result.values())}
 (root/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8");return manifest
if __name__=="__main__":
 logging.basicConfig(level=logging.INFO,format="%(levelname)s | %(message)s");o=args();print(json.dumps(export(o.output,o.batch_size,o.global_only,o.features_only,o.targets_only),default=str))
