"""Rebuild pilot rows left stale by the original insert-only full-market pass."""
from __future__ import annotations
import argparse,json
from build_stock_prediction_dataset import build_rows,load_context,load_prices,universe
from database.connection import connect

DEFAULT=("2330","2317","6488","2881")
def run(symbols):
 db=connect();result=[]
 try:
  options=argparse.Namespace(pilot=False,symbol=list(symbols),all=False)
  stocks=universe(db,options);context=load_context(db)
  for stock in stocks:
   groups=load_prices(db,[stock["stock_id"]]);features,targets=build_rows(db,[stock],groups,context);db.commit()
   result.append({"symbol":stock["symbol"],"features":features,"targets":targets})
  return result
 except Exception:db.rollback();raise
 finally:db.close()
if __name__=="__main__":
 parser=argparse.ArgumentParser();parser.add_argument("--symbol",action="append");args=parser.parse_args()
 print(json.dumps(run(args.symbol or DEFAULT)))
