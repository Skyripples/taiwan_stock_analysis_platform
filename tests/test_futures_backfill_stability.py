from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import backfill_futures_history as module


class Response:
    def __init__(self,status,headers=None,text=""):
        self.status_code=status;self.headers=headers or {};self.text=text;self.encoding="utf-8"
    def raise_for_status(self):
        if self.status_code>=400:raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    def __init__(self,responses):self.responses=list(responses);self.calls=0
    def post(self,*args,**kwargs):self.calls+=1;return self.responses.pop(0)


class FuturesBackfillStabilityTests(unittest.TestCase):
    def controller(self,responses,**kwargs):
        sleeps=[]
        controller=module.RequestController(Session(responses),delay=0,sleep=sleeps.append,clock=lambda:100,jitter=lambda a,b:0,**kwargs)
        return controller,sleeps

    def test_retry_after_and_exponential_backoff(self):
        controller,sleeps=self.controller([Response(429,{"Retry-After":"12"}),Response(200)])
        self.assertEqual(controller.post("x").status_code,200);self.assertEqual(sleeps,[12]);self.assertEqual(controller.retry_count,1)
        controller,sleeps=self.controller([Response(429),Response(429),Response(200)],cooldown_after=99)
        controller.post("x");self.assertEqual(sleeps,[5,10]);self.assertEqual(controller.rate_limit_count,2)

    def test_global_cooldown_and_exhaustion(self):
        controller,sleeps=self.controller([Response(429),Response(429),Response(429),Response(200)],cooldown_after=3,cooldown_seconds=90)
        controller.post("x");self.assertEqual(sleeps,[5,10,20,90])
        controller,_=self.controller([Response(429) for _ in range(6)],cooldown_after=99)
        with self.assertRaises(module.RateLimitExhausted):controller.post("x")
        self.assertEqual(controller.retry_count,5);self.assertEqual(controller.rate_limit_count,6)

    def test_resume_skips_success_and_retries_failed_gap(self):
        sessions=[date(2026,7,9),date(2026,7,13)]
        jobs=module.plan_jobs(["TX"],sessions,{"TX":{"2026-07-09"}},True)
        self.assertEqual(jobs,[("TX",date(2026,7,13))])

    def test_persistent_20260710_all_products_are_non_retryable_and_zero_network(self):
        day=date(2026,7,10);products=["TX","MTX","TMF"]
        self.assertEqual(module.plan_jobs(products,[day],{product:set() for product in products},True),[])
        entries=module.persistent_empty_entries(products,[day])
        self.assertEqual({entry["product"] for entry in entries},set(products))
        self.assertTrue(all(entry["status"]=="official-empty" and entry["retryable"] is False for entry in entries))
        options=argparse.Namespace(products=products,start="2026-07-10",end="2026-07-10",years=5,resume=True,dry_run=False,delay=0)
        session=Session([])
        with tempfile.TemporaryDirectory() as directory,patch.object(module,"STAGING",Path(directory)),patch.object(module,"CHECKPOINT",Path(directory)/"checkpoint.json"),patch.object(module,"arguments",return_value=options),patch.object(module,"get_trading_days_in_range",return_value=([day],[])),patch.object(module,"history_client",return_value=session):
            self.assertEqual(module.main(),0);self.assertEqual(session.calls,0)
            summary=json.loads((Path(directory)/"summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["skipped_persistent_official_empty"],3);self.assertEqual(summary["failures"],[])

    def test_tmf_listing_date_filters_planner_without_affecting_tx_mtx(self):
        sessions=[date(2024,7,26),date(2024,7,29)]
        existing={product:set() for product in ("TX","MTX","TMF")}
        jobs=module.plan_jobs(["TX","MTX","TMF"],sessions,existing,True)
        self.assertNotIn(("TMF",date(2024,7,26)),jobs)
        self.assertIn(("TMF",date(2024,7,29)),jobs)
        self.assertIn(("TX",date(2024,7,26)),jobs)
        self.assertIn(("MTX",date(2024,7,26)),jobs)
        self.assertEqual(module.pre_listing_count(["TX","MTX","TMF"],sessions),1)

    def test_tmf_pre_listing_main_makes_zero_network_requests(self):
        options=argparse.Namespace(products=["TMF"],start="2024-07-26",end="2024-07-26",years=5,resume=True,dry_run=False,delay=0)
        session=Session([])
        with tempfile.TemporaryDirectory() as directory,patch.object(module,"STAGING",Path(directory)),patch.object(module,"CHECKPOINT",Path(directory)/"checkpoint.json"),patch.object(module,"arguments",return_value=options),patch.object(module,"get_trading_days_in_range",return_value=([date(2024,7,26)],[])),patch.object(module,"history_client",return_value=session):
            self.assertEqual(module.main(),0)
            self.assertEqual(session.calls,0)
            summary=json.loads((Path(directory)/"summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["skipped_pre_listing"],1)
            self.assertEqual(summary["failures"],[])

    def test_atomic_partition_checkpoint_and_dedupe(self):
        options=argparse.Namespace(products=["TX"],start="2026-07-09",end="2026-07-09",years=5,resume=True,dry_run=False,delay=0)
        row={"trade_date":"2026-07-09","product":"TX","contract":"TX202607","session":"regular","close":1}
        with tempfile.TemporaryDirectory() as directory,patch.object(module,"STAGING",Path(directory)),patch.object(module,"CHECKPOINT",Path(directory)/"checkpoint.json"),patch.object(module,"arguments",return_value=options),patch.object(module,"get_trading_days_in_range",return_value=([date(2026,7,9)],[])),patch.object(module,"history_client",return_value=Session([])),patch.object(module,"fetch_day",return_value=[row]):
            self.assertEqual(module.main(),0)
            self.assertEqual(module.read_rows(module.path_for("TX",2026)),[row])
            self.assertEqual(json.loads(module.CHECKPOINT.read_text(encoding="utf-8"))["status"],"complete")
            self.assertEqual(module.main(),0)
            self.assertEqual(module.read_rows(module.path_for("TX",2026)),[row])

    def test_20260710_official_empty_is_not_contract_selection(self):
        parser_html='<input name="queryDate" value="2026/07/10"><table><tr><td>查無資料</td></tr></table>'
        controller=Mock();controller.post.return_value=Response(200,text=parser_html)
        with self.assertRaisesRegex(module.OfficialEmpty,"no TX market rows"):
            module.fetch_day(controller,date(2026,7,10),"TX")

if __name__=="__main__":unittest.main()
