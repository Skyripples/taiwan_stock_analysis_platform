"""DuckDB research layer for the local V3.14 Parquet lake."""
from __future__ import annotations
import argparse
from pathlib import Path
from config import PROJECT_ROOT
LAKE=PROJECT_ROOT/"data_lake"
def connect(lake=LAKE):
 import duckdb
 db=duckdb.connect();db.execute("SET threads=4");db.execute("SET memory_limit='4GB'")
 glob=lambda name:str(lake/name/"**"/"*.parquet").replace("\\","/")
 db.execute(f"CREATE VIEW prices AS SELECT * FROM read_parquet('{glob(Path('stock_daily_prices'))}',hive_partitioning=true,union_by_name=true)")
 db.execute(f"CREATE VIEW features AS SELECT * FROM read_parquet('{glob(Path('stock_prediction_features'))}',hive_partitioning=true,union_by_name=true)")
 db.execute(f"CREATE VIEW targets AS SELECT * FROM read_parquet('{glob(Path('stock_prediction_targets'))}',hive_partitioning=true,union_by_name=true)")
 db.execute(f"CREATE VIEW industry AS SELECT * FROM read_parquet('{glob(Path('stock_industry_daily_features'))}',hive_partitioning=true,union_by_name=true)")
 db.execute(f"CREATE VIEW stocks AS SELECT * FROM read_parquet('{glob(Path('dimensions/stocks'))}',hive_partitioning=true,union_by_name=true)")
 global_features=lake/"global_market"/"market_features"
 if global_features.exists():db.execute(f"CREATE VIEW global_features AS SELECT * FROM read_parquet('{glob(Path('global_market/market_features'))}',hive_partitioning=true,union_by_name=true)")
 return db
def examples(db,symbol):
 return {
  "single_stock":db.execute("SELECT * EXCLUDE(availability_json) FROM features WHERE symbol=? ORDER BY feature_date",[symbol]).to_arrow_table(),
  "cross_section":db.execute("SELECT symbol,feature_date,return_1d,rsi14 FROM features WHERE feature_date=(SELECT max(feature_date) FROM features)").to_arrow_table(),
  "ml_t1_sample":db.execute("SELECT f.symbol,f.feature_date,f.return_1d,f.rsi14,t.target_return,t.target_direction FROM features f JOIN targets t USING(symbol,feature_date) WHERE t.horizon=1 ORDER BY f.feature_date DESC LIMIT 100000").to_arrow_table(),
  "targets":db.execute("SELECT horizon,count(*) AS row_count,avg(target_direction) AS up_ratio FROM targets GROUP BY horizon ORDER BY horizon").to_arrow_table(),
  "global_join":db.execute("SELECT f.symbol,f.target_date,f.return_1d,g.feature_key,g.value FROM features f JOIN global_features g USING(target_date) WHERE f.symbol=? ORDER BY f.target_date DESC LIMIT 1000",[symbol]).to_arrow_table(),
 }
if __name__=="__main__":
 p=argparse.ArgumentParser();p.add_argument("--symbol",default="2330");o=p.parse_args();db=connect()
 for name,frame in examples(db,o.symbol).items():print(name,len(frame),frame.column_names[:12])
