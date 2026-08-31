"""Low-memory PostgreSQL storage audit for V3.14 research tables."""
from __future__ import annotations
import json, statistics
from config import PROJECT_ROOT as _PROJECT_ROOT
from database.connection import connect
TABLES=("stock_daily_prices","stock_prediction_features","stock_prediction_targets","stock_industry_daily_features")
def run():
 db=connect();out={}
 try:
  with db.cursor() as c:
   for table in TABLES:
    c.execute("SELECT reltuples::bigint estimated_rows,pg_relation_size(oid) heap,pg_indexes_size(oid) indexes,pg_total_relation_size(oid) total FROM pg_class WHERE oid=%s::regclass",(table,));out[table]=dict(c.fetchone())
    c.execute(f"SELECT count(*) exact_rows FROM {table}");out[table].update(c.fetchone())
    out[table]["bytes_per_row"]=out[table]["total"]/max(1,out[table]["exact_rows"])
   c.execute("SELECT avg(pg_column_size(features)) avg_jsonb,max(pg_column_size(features)) max_jsonb,avg((SELECT count(*) FROM jsonb_object_keys(features))) avg_keys FROM stock_prediction_features TABLESAMPLE SYSTEM(0.1) WHERE feature_version='v3.14-1.0'");out["feature_jsonb_sample"]=dict(c.fetchone())
   c.execute("SELECT features FROM stock_prediction_features WHERE feature_version='v3.14-1.0' AND target_date IS NOT NULL ORDER BY feature_date DESC LIMIT 1")
   out["complete_feature_keys"]=sorted(c.fetchone()["features"])
   c.execute("SELECT pg_database_size(current_database()) size");out["database_bytes"]=c.fetchone()["size"]
 finally:db.close()
 return out
if __name__=="__main__":print(json.dumps(run(),default=float))
