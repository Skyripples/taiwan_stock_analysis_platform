from __future__ import annotations

import argparse
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))

import backfill_stock_history as backfill
from stock_history_metrics import chips_summary
from update_stock_data import (
    parse_tpex_institutional, parse_tpex_margin,
    parse_twse_institutional, parse_twse_margin,
)


def table(title, rows):
    return {"tables":[{"title":title,"fields":[],"data":rows}]}


class StockChipsBackfillTests(unittest.TestCase):
    def test_four_official_parsers_and_nulls(self):
        fields=["證券代號","外陸資買進股數(不含外資自營商)","外陸資賣出股數(不含外資自營商)","外陸資買賣超股數(不含外資自營商)","投信買進股數","投信賣出股數","投信買賣超股數","自營商買進股數","自營商賣出股數","自營商買賣超股數","三大法人買賣超股數"]
        twse=parse_twse_institutional({"stat":"OK","fields":fields,"data":[["2330","10","4","6","3","1","2","5","2","3","11"]]},"2026-09-01")
        self.assertEqual(twse["2330"]["foreign_net"],6)
        self.assertEqual(parse_twse_margin(table("融資融券彙總",[["2330","台積電",0,0,0,"100","110",0,0,0,0,"20","18"]]),"2026-09-01")["2330"]["margin_change"],10)
        tpex_row=["6488","環球晶","10","4","6",0,0,0,0,0,0,"3","1","2",0,0,0,0,0,0,"5","2","3","11"]
        self.assertEqual(parse_tpex_institutional(table("三大法人買賣明細",[tpex_row]),"2026-09-01")["6488"]["dealer_net"],3)
        credit=["6488","環球晶",0,0,0,"100","--",0,0,0,0,0,0,"20","18"]
        self.assertIsNone(parse_tpex_margin(table("融資融券餘額",[credit]),"2026-09-01")["6488"]["margin_balance"])

    def test_twse_official_no_data_is_empty_not_parser_failure(self):
        payload={"stat":"查詢狀況，沒有符合條件的資料"}
        self.assertEqual(parse_twse_institutional(payload,"2026-07-10"),{})
        self.assertEqual(parse_twse_margin(payload,"2026-07-10"),{})

    def test_verified_persistent_empty_skips_network_on_resume(self):
        options=argparse.Namespace(dataset="chips",start="2026-07-10",end="2026-07-10",years=5,symbols=["2330"],resume=True,dry_run=False,refresh=False,audit_only=False,delay=0)
        fetch=Mock(side_effect=AssertionError("persistent official-empty must not request network"))
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)),patch.object(backfill,"arguments",return_value=options),patch.object(backfill,"universe",return_value=({"2330":"TWSE"},{})),patch.object(backfill,"eligible_completed_trading_days",return_value=([date(2026,7,10)],[],[])),patch("update_stock_data.fetch_chips_component",fetch),patch("update_stock_data.OfficialClient",return_value=object()):
            self.assertEqual(backfill.main(),0);fetch.assert_not_called()
            status=__import__("json").loads((Path(directory)/"chips_status.json").read_text(encoding="utf-8"))["partitions"]
            self.assertFalse(status["2026-07-10|TWSE|institutional"]["retryable"])
            self.assertFalse(status["2026-07-10|TWSE|margin"]["retryable"])

    def test_resume_is_component_aware_and_second_run_has_zero_network(self):
        options=argparse.Namespace(dataset="chips",start="2026-09-01",end="2026-09-01",years=5,symbols=["2330","6488"],resume=True,dry_run=False,refresh=False,audit_only=False,delay=0)
        def fetched(client,market,component,day):
            symbol="2330" if market=="TWSE" else "6488"
            return {symbol:{"trade_date":day,"foreign_net":1,"investment_trust_net":2,"dealer_net":3,"institutional_total":6}} if component=="institutional" else {symbol:{"trade_date":day,"margin_balance":100,"margin_change":1,"short_balance":20,"short_change":-1}}
        call=Mock(side_effect=fetched)
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)),patch.object(backfill,"arguments",return_value=options),patch.object(backfill,"universe",return_value=({"2330":"TWSE","6488":"TPEx"},{})),patch.object(backfill,"eligible_completed_trading_days",return_value=([date(2026,9,1)],[],[])),patch("update_stock_data.fetch_chips_component",call),patch("update_stock_data.OfficialClient",return_value=object()):
            self.assertEqual(backfill.main(),0);self.assertEqual(call.call_count,4)
            self.assertEqual(backfill.main(),0);self.assertEqual(call.call_count,4)
            summary=__import__("json").loads((Path(directory)/"summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["downloaded_rows"],0);self.assertEqual(summary["duplicates"],0);self.assertEqual(summary["skipped_existing"],4)

    def test_failed_component_retries_and_preserves_success(self):
        existing=[];now="2026-09-01T10:00:00+00:00"
        merged,_=backfill.merge_chips_component(existing,{"2330":{"foreign_net":9}},date(2026,9,1),"TWSE","institutional",now)
        unchanged,_=backfill.merge_chips_component(merged,{"2330":{"margin_balance":None}},date(2026,9,1),"TWSE","margin",now)
        self.assertEqual(unchanged[0]["foreign_net"],9);self.assertIsNone(unchanged[0]["margin_balance"])
        status={"partitions":{"2026-09-01|TWSE|institutional":{"status":"success"},"2026-09-01|TWSE|margin":{"status":"retryable-error"}}}
        self.assertEqual(status["partitions"]["2026-09-01|TWSE|margin"]["status"],"retryable-error")

    def test_completed_day_holiday_and_weekend_planning(self):
        completed,closed,pending=backfill.eligible_completed_trading_days(date(2026,9,25),date(2026,10,10),datetime.fromisoformat("2026-10-14T02:00:00+08:00"))
        self.assertNotIn(date(2026,9,25),completed);self.assertNotIn(date(2026,9,28),completed);self.assertNotIn(date(2026,10,9),completed);self.assertNotIn(date(2026,10,10),completed)
        self.assertFalse(pending);self.assertTrue(closed)

    def test_metrics_and_offline_audit(self):
        rows=[]
        for i in range(1,62):rows.append({"trade_date":f"2026-01-{i:02d}","foreign_net":i,"investment_trust_net":1,"institutional_total":i+1,"margin_balance":100+i,"short_balance":50+i})
        result=chips_summary(rows)
        self.assertIsNotNone(result["foreign_60d"]);self.assertIn("margin_short_ratio",result)
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)):
            path=backfill.partition("chips","2026-09")
            backfill.write_rows(path,[{"symbol":"2330","trade_date":"2026-09-01","foreign_net":1}],("symbol","trade_date"))
            audit=backfill.audit_chips_staging(date(2026,9,1),date(2026,9,30),{"2330":"TWSE"})
            self.assertEqual(audit["duplicate_keys"],0);self.assertEqual(audit["rows"],1);self.assertEqual(audit["partial_rows"][0]["missing_components"],["margin"])


if __name__=="__main__":unittest.main()
