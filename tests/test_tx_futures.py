import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
SPEC=importlib.util.spec_from_file_location("update_tx_futures",ROOT/"scripts"/"update_tx_futures.py")
MODULE=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)

def row(month,close="22000"):
    return ["TX",month,"21900","22100","21800",close,"▲100","▲0.46%","10","20","30","21950","40000","0","0","0","0"]

class TxFuturesTest(unittest.TestCase):
    def test_selects_nearest_unexpired_months(self):
        rows=[row("202609/202610"),row("202608"),row("202611","22200"),row("202609","22100"),row("202612","22300"),["MTX","202609"]]
        selected=MODULE.select_contracts(rows,date(2026,9,9))
        self.assertEqual([item["contract_code"] for item in selected],["202609","202611"])
        self.assertEqual(selected[0]["change"],100)

    def test_expired_front_month_is_rejected(self):
        selected=MODULE.select_contracts([row("202609"),row("202610")],date(2026,9,17))
        self.assertEqual(selected[0]["contract_code"],"202610")

    def test_schema_and_failure_protection(self):
        payload=MODULE.build_payload({"trade_date":"2026-09-09","contracts":MODULE.select_contracts([row("202609"),row("202610")],date(2026,9,9))},datetime(2026,9,10,tzinfo=timezone.utc)); MODULE.validate(payload)
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/"tx.json"; MODULE.atomic_write(payload,output); before=output.read_bytes()
            with self.assertRaises(ValueError): MODULE.validate({"dataset":"tx_futures"})
            self.assertEqual(output.read_bytes(),before)

    def test_page_load_contract(self):
        html=(ROOT/"futures.html").read_text(encoding="utf-8"); js=(ROOT/"futures.js").read_text(encoding="utf-8")
        for token in ("台指期 TX","近月／次月比較","tx_futures.json","目前無法取得資料"): self.assertIn(token,html+js)

if __name__=="__main__": unittest.main()
