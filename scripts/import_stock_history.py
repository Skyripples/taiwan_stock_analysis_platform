"""Idempotently import local stock-history staging files into PostgreSQL."""
from __future__ import annotations
import argparse,gzip,json
from pathlib import Path
from typing import Any
from config import PROJECT_ROOT
from database.connection import connect

ROOT=PROJECT_ROOT/"data_lake"/"staging"/"stock_history"
SPECS={
 "revenue":("stock_monthly_revenue","revenue_month",("revenue","revenue_yoy","revenue_mom")),
 "financials":("stock_financials","period_end",("fiscal_year","quarter","statement_scope","period_end","published_date","available_date","eps","revenue","net_income","gross_margin","operating_margin","net_margin","roe","total_assets","total_liabilities","debt_ratio","current_ratio","operating_cash_flow","investing_cash_flow","capital_expenditure","free_cash_flow")),
 "valuation":("stock_valuations","valuation_date",("valuation_date","pe","pb","dividend_yield")),
 "chips":("stock_chips","trade_date",("trade_date","foreign_net","investment_trust_net","dealer_net","institutional_total","margin_balance","margin_change","short_balance","short_change")),
}

def args():
 p=argparse.ArgumentParser(); p.add_argument("--dataset",choices=(*SPECS,"all"),default="all"); p.add_argument("--dry-run",action="store_true"); p.add_argument("--batch-size",type=int,default=1000); return p.parse_args()
def rows(dataset):
 for path in sorted((ROOT/dataset).glob("*.jsonl.gz")):
  with gzip.open(path,"rt",encoding="utf-8") as stream:
   for line in stream:
    if line.strip(): yield json.loads(line)
def main():
 o=args(); selected=SPECS if o.dataset=="all" else (o.dataset,); material={name:list(rows(name)) for name in selected}
 print(json.dumps({"dry_run":o.dry_run,"rows":{key:len(value) for key,value in material.items()}},ensure_ascii=False))
 if o.dry_run:return 0
 from psycopg.types.json import Jsonb
 connection=connect()
 try:
  with connection.transaction(),connection.cursor() as cursor:
   for dataset,items in material.items():
    table,date_field,fields=SPECS[dataset]
    if dataset=="revenue": fields=("revenue_month",*fields)
    for offset in range(0,len(items),o.batch_size):
     for row in items[offset:offset+o.batch_size]:
      cursor.execute("SELECT stock_id FROM stocks WHERE symbol=%s",(row["symbol"],)); stock=cursor.fetchone()
      if not stock: continue
      columns=("stock_id",*fields,"source","source_date","fetched_at","source_payload")
      values=(stock["stock_id"],*(row.get(field) for field in fields),row.get("source"),row.get("source_date"),row.get("fetched_at"),Jsonb(row))
      conflict=("stock_id,fiscal_year,quarter,statement_scope" if dataset=="financials" else f"stock_id,{date_field}")
      updates=[field for field in columns if field not in {"stock_id","fiscal_year","quarter","statement_scope",date_field}]
      statement=f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s']*len(columns))}) ON CONFLICT ({conflict}) DO UPDATE SET "+",".join(f"{field}=EXCLUDED.{field}" for field in updates)+f" WHERE (EXCLUDED.source_date IS NOT NULL AND ({table}.source_date IS NULL OR EXCLUDED.source_date >= {table}.source_date)) OR (EXCLUDED.source_date IS NULL AND {table}.source_date IS NULL AND ({table}.fetched_at IS NULL OR EXCLUDED.fetched_at >= {table}.fetched_at))"
      cursor.execute(statement,values)
  connection.commit()
 except Exception:
  connection.rollback(); raise
 finally: connection.close()
 return 0
if __name__=="__main__":raise SystemExit(main())
