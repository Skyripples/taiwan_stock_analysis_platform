"""Truncate migrated V3.14 research tables after every safety gate has passed."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from maintenance.audit_v314_dependencies import run as dependency_audit
from database.connection import connect

TABLES=("stock_prediction_targets","stock_prediction_features","stock_industry_daily_features","stock_daily_prices")
def validated(report):
 if report.get("temporal_leakage") != 0:return False
 if report.get("sample_validation",{}).get("failed") != 0:return False
 for details in report.get("datasets",{}).values():
  if not details.get("row_count_match") or details.get("pk_duplicates") != 0:return False
 return True
def main():
 parser=argparse.ArgumentParser();parser.add_argument("--validation-report",type=Path,required=True);parser.add_argument("--execute",action="store_true");args=parser.parse_args()
 report=json.loads(args.validation_report.read_text(encoding="utf-8"));dependencies=dependency_audit()
 external={table:value for table,value in dependencies.items() if value["dependent_views"] or value["dependent_functions"]}
 if not validated(report):raise SystemExit("Refusing cleanup: migration validation gates did not pass")
 if external:raise SystemExit(f"Refusing cleanup: database dependencies remain: {external}")
 if not args.execute:
  print(json.dumps({"status":"dry_run","tables":TABLES,"dependencies":dependencies}));return
 db=connect()
 try:
  with db.cursor() as cursor:
   cursor.execute("SELECT pg_database_size(current_database()) size");before=cursor.fetchone()["size"]
   # Keep the empty schema/migrations intact while releasing all research data
   # and index pages. This is safer than DROP for future incremental tooling.
   cursor.execute("TRUNCATE "+",".join(TABLES))
   cursor.execute("DROP INDEX IF EXISTS idx_v314_export_feature_date")
   cursor.execute("DROP INDEX IF EXISTS idx_v314_export_target_feature_date")
  db.commit()
  with db.cursor() as cursor:cursor.execute("SELECT pg_database_size(current_database()) size");after=cursor.fetchone()["size"]
  print(json.dumps({"status":"complete","truncated":TABLES,"database_before_bytes":before,"database_after_bytes":after,"reclaimed_bytes":before-after,"schema_retained":True}))
 except Exception:db.rollback();raise
 finally:db.close()
if __name__=="__main__":main()
