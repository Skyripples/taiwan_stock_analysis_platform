from __future__ import annotations
import argparse,json,subprocess,sys,tempfile,unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from stock_history_metrics import chips_summary,revenue_metrics,valuation_percentiles
from api.routes.stocks import router
import backfill_stock_history
from backfill_stock_history import eligible_revenue_months,read_rows,universe,write_rows

class StockHistoryExpansionTests(unittest.TestCase):
 def test_revenue_metrics_keep_null_and_calculate_growth(self):
  rows=[]
  for year,factor in ((2024,1),(2025,2)):
   for month in range(1,4): rows.append({"revenue_month":f"{year}-{month:02d}-01","revenue":100*factor+month,"revenue_yoy":None,"revenue_mom":None})
  result=revenue_metrics(rows); latest=result[-1]
  self.assertIsNotNone(latest["yoy"]); self.assertIsNotNone(latest["average_yoy_3m"]); self.assertEqual(latest["consecutive_positive_yoy_months"],3)
 def test_chips_windows_and_missing_data(self):
  rows=[{"trade_date":f"2026-01-{day:02d}","foreign_net":day,"investment_trust_net":1,"institutional_total":day+1,"margin_balance":100+day,"short_balance":50+day} for day in range(1,22)]
  result=chips_summary(rows); self.assertEqual(result["foreign_5d"],sum(range(17,22))); self.assertEqual(result["margin_20d_change"],20); self.assertIsNone(result["foreign_60d"])
 def test_valuation_percentiles(self):
  rows=[{"valuation_date":f"2026-01-0{i}","pe":i,"pb":i*2,"dividend_yield":6-i} for i in range(1,6)]
  self.assertEqual(valuation_percentiles(rows)["pe"],90.0)
 def test_additive_idempotent_migration_and_routes(self):
  migration=(ROOT/"scripts/database/migrations/007_stock_history_metadata.sql").read_text(encoding="utf-8")
  self.assertGreaterEqual(migration.count("IF NOT EXISTS"),8)
  routes=(ROOT/"scripts/api/routes/stocks.py").read_text(encoding="utf-8")
  for path in ("/revenue","/valuation/history","/chips/history","/margin/history"):self.assertIn(path,routes)
 def test_backfill_dry_run_has_zero_network(self):
  run=subprocess.run([sys.executable,str(ROOT/"scripts/backfill_stock_history.py"),"--dataset","all","--start","2026-01-01","--end","2026-01-31","--symbols","2330","2454","--dry-run"],cwd=ROOT,text=True,capture_output=True,check=True)
  self.assertIn('"network_requests": 0',run.stdout); self.assertIn('"symbols": 2',run.stdout)
 def test_revenue_backfill_excludes_unfinished_current_month(self):
  periods=eligible_revenue_months(date(2026,8,1),date(2026,10,12),today=date(2026,10,12))
  self.assertEqual(periods,[date(2026,8,1),date(2026,9,1)])
 def test_revenue_resume_skips_complete_partition_without_network(self):
  options=argparse.Namespace(dataset="revenue",start="2026-01-01",end="2026-01-31",years=5,symbols=["2330","6488"],resume=True,dry_run=False,refresh=False,delay=0)
  rows=[{"symbol":symbol,"revenue_month":"2026-01-01","revenue":100,"source_date":None} for symbol in options.symbols]
  with tempfile.TemporaryDirectory() as directory, patch.object(backfill_stock_history,"STAGING",Path(directory)), patch.object(backfill_stock_history,"arguments",return_value=options), patch.object(backfill_stock_history,"universe",return_value=({"2330":"TWSE","6488":"TPEx"},{})), patch.object(backfill_stock_history,"fetch_revenue",return_value=rows) as fetch, patch("update_stock_data.OfficialClient",return_value=object()):
   self.assertEqual(backfill_stock_history.main(),0)
   self.assertEqual(backfill_stock_history.main(),0)
   self.assertEqual(fetch.call_count,1)
   summary=json.loads((Path(directory)/"summary.json").read_text(encoding="utf-8"))
   self.assertEqual(summary["downloaded_rows"],0)
   self.assertEqual(summary["skipped_existing"],2)
   self.assertEqual(summary["duplicates"],0)
 def test_twse_tpex_sample_universe(self):
  markets,_=universe(["2330","2454","6488"])
  self.assertEqual(markets["2330"],"TWSE"); self.assertEqual(markets["2454"],"TWSE"); self.assertEqual(markets["6488"],"TPEx")
 def test_staging_write_is_atomic_and_deduplicated(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/"sample.jsonl.gz"; rows=[{"symbol":"2330","trade_date":"2026-01-02","value":1},{"symbol":"2330","trade_date":"2026-01-02","value":2}]
   write_rows(path,rows,("symbol","trade_date")); self.assertEqual(read_rows(path),[rows[-1]])
 def test_history_api_and_range_validation(self):
  app=FastAPI(); app.include_router(router,prefix="/api/v1"); client=TestClient(app)
  with patch("api.routes.stocks.get_revenue_history",return_value={"symbol":"2330","count":1,"revenue":[]}):
   self.assertEqual(client.get("/api/v1/stocks/2330/revenue?start=2025-01-01&end=2026-01-01").status_code,200)
  self.assertEqual(client.get("/api/v1/stocks/2330/revenue?start=2026-02-01&end=2026-01-01").status_code,422)

if __name__=="__main__":unittest.main()
