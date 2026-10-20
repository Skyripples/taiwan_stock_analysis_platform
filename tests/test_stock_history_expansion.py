from __future__ import annotations
import argparse,json,subprocess,sys,tempfile,unittest
import requests
from datetime import date,datetime
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from stock_history_metrics import chips_summary,revenue_metrics,valuation_percentiles
from api.routes.stocks import router
from trading_calendar import get_trading_days_in_range
import backfill_stock_history
from backfill_stock_history import (
 ValuationOfficialEmpty, ValuationOfficialPartial, ValuationSourceError, eligible_revenue_months,eligible_valuation_days,
 fetch_valuation, official_json, parse_tpex_valuation, read_rows, roc_date, universe, write_rows,
 valuation_trading_days,
)

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
  short=[{"valuation_date":f"2026-01-{i:02d}","pe":i,"pb":i*2,"dividend_yield":21-i} for i in range(1,6)]
  self.assertIsNone(valuation_percentiles(short)["pe"])
  rows=[{"valuation_date":f"2026-01-{i:02d}","pe":i,"pb":i*2,"dividend_yield":21-i} for i in range(1,21)]
  result=valuation_percentiles(rows)
  self.assertEqual(result["pe"],97.5); self.assertEqual(result["dividend_yield"],2.5)
  rows[-1]["pe"]=-3
  self.assertEqual(valuation_percentiles(rows)["pe"],2.5)
 def test_tpex_valid_day_fixture_and_null_values(self):
  fields=["股票代號","公司名稱","本益比","每股股利","股利年度","殖利率(%)","股價淨值比","財報年/季"]
  payload={"tables":[{"date":"115/10/08","fields":fields,"data":[["6488","環球晶","--","1",114,"N/A","","115Q2"]]}]}
  rows=parse_tpex_valuation(payload,date(2026,10,8),{"6488":"TPEx"})
  self.assertEqual(rows,[{"symbol":"6488","valuation_date":"2026-10-08","pe":None,"dividend_yield":None,"pb":None,"source":"TPEx peQryDate","source_date":"2026-10-08"}])
 def test_roc_date_and_non_trading_day(self):
  self.assertEqual(roc_date("115/10/08"),date(2026,10,8)); self.assertIsNone(roc_date("bad"))
  payload={"tables":[{"date":"115/10/08","fields":["股票代號","本益比","殖利率(%)","股價淨值比"],"data":[]}]}
  with self.assertRaises(ValuationOfficialEmpty): parse_tpex_valuation(payload,date(2026,10,10),{"6488":"TPEx"})
 def test_tpex_malformed_response(self):
  with self.assertRaises(ValuationSourceError): parse_tpex_valuation({"tables":[]},date(2026,10,8),{"6488":"TPEx"})
  class Response:
   headers={"content-type":"text/html"}
   def raise_for_status(self): pass
   def json(self): raise AssertionError("HTML response must not be parsed as JSON")
  class Session:
   def get(self,*args,**kwargs): return Response()
  client=type("Client",(),{"session":Session(),"delay":0,"last_request":0})()
  with self.assertRaises(ValuationSourceError): official_json(client,"https://example.invalid",{})
 def test_twse_regression_and_tpex_endpoint(self):
  fields=["股票代號","公司名稱","本益比","每股股利","股利年度","殖利率(%)","股價淨值比","財報年/季"]
  class Client:
   def json(self,url,params=None):
    if "BWIBBU_d" in url: return {"stat":"OK","data":[["2330","台積電","x","1.5","x","20","5"]]}
    self.url=url; self.params=params
    return {"tables":[{"date":"115/10/08","fields":fields,"data":[["6488","環球晶","15","1",114,"2.5","3","115Q2"]]}]}
  client=Client(); rows=fetch_valuation(client,date(2026,10,8),{"2330":"TWSE","6488":"TPEx"})
  self.assertEqual({row["symbol"] for row in rows},{"2330","6488"})
  self.assertTrue(client.url.endswith("/afterTrading/peQryDate")); self.assertEqual(client.params["date"],"115/10/08")
 def test_additive_idempotent_migration_and_routes(self):
  migration=(ROOT/"scripts/database/migrations/007_stock_history_metadata.sql").read_text(encoding="utf-8")
  self.assertGreaterEqual(migration.count("IF NOT EXISTS"),8)
  routes=(ROOT/"scripts/api/routes/stocks.py").read_text(encoding="utf-8")
  for path in ("/revenue","/valuation","/chips/history","/margin/history"):self.assertIn(path,routes)
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
 def test_valuation_resume_skips_complete_partition_without_network(self):
  options=argparse.Namespace(dataset="valuation",start="2026-01-02",end="2026-01-02",years=5,symbols=["2330","6488"],resume=True,dry_run=False,refresh=False,delay=0)
  rows=[{"symbol":symbol,"valuation_date":"2026-01-02","source_date":"2026-01-02","pe":10,"source":"TWSE BWIBBU_d" if symbol=="2330" else "TPEx peQryDate"} for symbol in options.symbols]
  with tempfile.TemporaryDirectory() as directory, patch.object(backfill_stock_history,"STAGING",Path(directory)), patch.object(backfill_stock_history,"arguments",return_value=options), patch.object(backfill_stock_history,"universe",return_value=({"2330":"TWSE","6488":"TPEx"},{})), patch.object(backfill_stock_history,"fetch_valuation") as fetch, patch("update_stock_data.OfficialClient",return_value=object()):
   backfill_stock_history.write_rows(backfill_stock_history.partition("valuation","2026-01"),rows,("symbol","valuation_date"))
   self.assertEqual(backfill_stock_history.main(),0); fetch.assert_not_called()
   summary=json.loads((Path(directory)/"summary.json").read_text(encoding="utf-8"))
   self.assertEqual(summary["skipped_existing"],2)
 def test_valuation_today_requires_taipei_close(self):
  before=datetime.fromisoformat("2026-10-14T02:02:00+08:00")
  completed,_,pending=eligible_valuation_days(date(2026,10,13),date(2026,10,14),before)
  self.assertIn(date(2026,10,13),completed);self.assertNotIn(date(2026,10,14),completed);self.assertIn(date(2026,10,14),pending)
  after=datetime.fromisoformat("2026-10-14T15:31:00+08:00")
  completed,_,pending=eligible_valuation_days(date(2026,10,14),date(2026,10,14),after)
  self.assertEqual(completed,[date(2026,10,14)]);self.assertEqual(pending,[])
 def test_persistent_official_partial_is_saved_once(self):
  options=argparse.Namespace(dataset="valuation",start="2026-07-10",end="2026-07-10",years=5,symbols=["2330","6488"],resume=True,dry_run=False,refresh=False,delay=0)
  partial_row={"symbol":"6488","valuation_date":"2026-07-10","source_date":"2026-07-10","pe":10,"source":"TPEx peQryDate"}
  with tempfile.TemporaryDirectory() as directory,patch.object(backfill_stock_history,"STAGING",Path(directory)),patch.object(backfill_stock_history,"arguments",return_value=options),patch.object(backfill_stock_history,"universe",return_value=({"2330":"TWSE","6488":"TPEx"},{})),patch.object(backfill_stock_history,"eligible_valuation_days",return_value=([date(2026,7,10)],[],[])),patch.object(backfill_stock_history,"fetch_valuation",side_effect=ValuationOfficialPartial(["TWSE"],[partial_row])) as fetch,patch("update_stock_data.OfficialClient",return_value=object()):
   self.assertEqual(backfill_stock_history.main(),2);self.assertEqual(fetch.call_count,1)
   first=json.loads((Path(directory)/"summary.json").read_text(encoding="utf-8"));self.assertEqual(first["downloaded_rows"],1);self.assertEqual(first["duplicates"],0);self.assertEqual(first["failures"][0]["error"],"official_partial")
   self.assertEqual(backfill_stock_history.main(),0);self.assertEqual(fetch.call_count,1)
   second=json.loads((Path(directory)/"summary.json").read_text(encoding="utf-8"));self.assertEqual(second["downloaded_rows"],0);self.assertEqual(second["duplicates"],0);self.assertEqual(second["skipped_official_partial"],1)
 def test_http_error_remains_retryable_gap(self):
  options=argparse.Namespace(dataset="valuation",start="2023-09-13",end="2023-09-13",years=5,symbols=["2330"],resume=True,dry_run=False,refresh=False,delay=0)
  with tempfile.TemporaryDirectory() as directory,patch.object(backfill_stock_history,"STAGING",Path(directory)),patch.object(backfill_stock_history,"arguments",return_value=options),patch.object(backfill_stock_history,"universe",return_value=({"2330":"TWSE"},{})),patch.object(backfill_stock_history,"eligible_valuation_days",return_value=([date(2023,9,13)],[],[])),patch.object(backfill_stock_history,"fetch_valuation",side_effect=requests.HTTPError("temporary")),patch("update_stock_data.OfficialClient",return_value=object()):
   self.assertEqual(backfill_stock_history.main(),2)
   summary=json.loads((Path(directory)/"summary.json").read_text(encoding="utf-8"));self.assertTrue(summary["failures"][0]["retryable"])
 def test_valuation_uses_official_calendar_before_network(self):
  sessions,skipped=valuation_trading_days(date(2026,9,25),date(2026,10,10))
  self.assertNotIn(date(2026,9,25),sessions)
  self.assertNotIn(date(2026,9,28),sessions)
  self.assertNotIn(date(2026,10,9),sessions)
  self.assertNotIn(date(2026,10,10),sessions)
  self.assertIn(date(2026,9,29),sessions)
  self.assertTrue({date(2026,9,25),date(2026,9,28),date(2026,10,9),date(2026,10,10)}.issubset(set(skipped)))
  options=argparse.Namespace(dataset="valuation",start="2026-09-25",end="2026-10-10",years=5,symbols=["2330"],resume=False,dry_run=False,refresh=False,delay=0)
  rows=lambda client,day,markets:[{"symbol":"2330","valuation_date":day.isoformat(),"source_date":day.isoformat(),"pe":10}]
  with tempfile.TemporaryDirectory() as directory, patch.object(backfill_stock_history,"STAGING",Path(directory)), patch.object(backfill_stock_history,"arguments",return_value=options), patch.object(backfill_stock_history,"universe",return_value=({"2330":"TWSE"},{})), patch.object(backfill_stock_history,"fetch_valuation",side_effect=rows) as fetch, patch("update_stock_data.OfficialClient",return_value=object()):
   self.assertEqual(backfill_stock_history.main(),0)
   requested={call.args[1] for call in fetch.call_args_list}
   self.assertIn(date(2026,9,29),requested)
   self.assertTrue({date(2026,9,25),date(2026,9,28),date(2026,10,9),date(2026,10,10)}.isdisjoint(requested))
   summary=json.loads((Path(directory)/"summary.json").read_text(encoding="utf-8"))
   self.assertEqual(summary["skipped_non_trading_days"],8)
   self.assertEqual(summary["official_empty"],[]); self.assertEqual(summary["failures"],[])
 def test_historical_pit_calendar_coverage_and_holidays(self):
  normal_days=[date(2021,10,13),date(2022,3,1),date(2023,3,1),date(2024,3,1),date(2025,3,3),date(2026,3,2)]
  for day in normal_days:
   sessions,skipped=valuation_trading_days(day,day)
   self.assertEqual(sessions,[day]); self.assertEqual(skipped,[])
  holidays=[date(2022,2,1),date(2023,1,23),date(2024,2,9),date(2025,1,29),date(2026,9,25),date(2026,9,28),date(2026,10,9)]
  for day in holidays:
   sessions,skipped=valuation_trading_days(day,day)
   self.assertEqual(sessions,[]); self.assertEqual(skipped,[day])
  sessions,skipped=valuation_trading_days(date(2024,3,2),date(2024,3,2))
  self.assertEqual(sessions,[]); self.assertEqual(skipped,[date(2024,3,2)])
 def test_calendar_coverage_gap_fails_closed(self):
  with tempfile.TemporaryDirectory() as directory:
   missing=Path(directory)/"missing.parquet"
   with self.assertRaisesRegex(ValueError,"coverage is missing 2021-10-13"):
    get_trading_days_in_range(date(2021,10,13),date(2021,10,13),pit_glob=missing,calendar={"years":{}})
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
