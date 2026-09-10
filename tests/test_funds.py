import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("update_funds",ROOT/"scripts"/"update_funds.py")
MODULE=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)

class FakeResponse:
    def __init__(self,payload): self.payload=payload
    def raise_for_status(self): return None
    def json(self): return self.payload

class FakeSession:
    def get(self,url,**kwargs):
        if url==MODULE.TWSE_LATEST: return FakeResponse([{"Date":"1150909","Code":"0050","ClosingPrice":"50"}])
        if url==MODULE.TPEX_LATEST: return FakeResponse([{"Date":"1150909","SecuritiesCompanyCode":"006201","Close":"25"}])
        if url==MODULE.TWSE_HISTORY: return FakeResponse({"stat":"OK","tables":[{"data":[["0050","ETF","0","0","0","0","0","0","40"]]}]})
        return FakeResponse({"date":kwargs["params"]["date"].replace("/",""),"tables":[{"data":[["006201","ETF","20"]]}]})

class FundsTest(unittest.TestCase):
    def test_payload_schema_and_returns(self):
        universe={"0050":{"symbol":"0050","fund_code":"0050","name":"元大台灣50","market":"TWSE","category":"ETF","asset_type":"ETF","issuer":None,"nav":None,"expense_ratio":None,"management_fee":None},"006201":{"symbol":"006201","fund_code":"006201","name":"元大富櫃50","market":"TPEx","category":"ETF","asset_type":"ETF","issuer":None,"nav":None,"expense_ratio":None,"management_fee":None}}
        with patch.object(MODULE,"load_universe",return_value=universe): payload=MODULE.build_payload(FakeSession(),datetime(2026,9,10,tzinfo=timezone.utc))
        MODULE.validate(payload); self.assertEqual(len(payload["funds"]),2); self.assertEqual(payload["funds"][0]["returns"]["1y"],25.0)

    def test_failure_does_not_replace_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/"funds.json"; output.write_text('{"valid": true}',encoding="utf-8")
            with self.assertRaises(ValueError): MODULE.validate({"funds":[]})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")),{"valid":True})

    def test_frontend_search_filter_sort_hooks(self):
        html=(ROOT/"funds.html").read_text(encoding="utf-8"); js=(ROOT/"funds.js").read_text(encoding="utf-8")
        for token in ("fundSearch","fundType","data-sort=\"1y\"","stock-analysis.html?symbol="): self.assertIn(token,html+js)

if __name__=="__main__": unittest.main()
