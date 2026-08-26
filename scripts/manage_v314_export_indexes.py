"""Create/drop temporary indexes used only by the V3.14 Parquet export."""
import argparse
from database.connection import connect
def main():
 p=argparse.ArgumentParser();p.add_argument("action",choices=("create","drop"));o=p.parse_args();db=connect()
 try:
  db.autocommit=True
  with db.cursor() as c:
   if o.action=="create":
    c.execute("CREATE INDEX IF NOT EXISTS idx_v314_export_feature_date ON stock_prediction_features(feature_date) WHERE feature_version='v3.14-1.0'")
    c.execute("CREATE INDEX IF NOT EXISTS idx_v314_export_target_feature_date ON stock_prediction_targets(feature_date) WHERE target_version='v3.14-1.0'")
   else:
    c.execute("DROP INDEX IF EXISTS idx_v314_export_feature_date");c.execute("DROP INDEX IF EXISTS idx_v314_export_target_feature_date")
 finally:db.close()
if __name__=="__main__":main()
