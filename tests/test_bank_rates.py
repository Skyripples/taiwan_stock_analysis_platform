from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import update_bank_rates as rates


FIXTURE = {
    "data": [
        ["004臺　銀", "活期存款", "--", "一般", "--", "0.705"],
        ["004臺　銀", "活期儲蓄存款", "--", "一般", "--", "0.825"],
        ["004臺　銀", "定期存款", "1年", "一般", "1.700", "1.690"],
        ["004臺　銀", "定期存款", "1年", "5百萬", "0.770", "0.770"],
        ["004臺　銀", "定期儲蓄存款", "1年", "一般", "1.725", "1.715"],
        ["004臺　銀", "放款利率", "1年", "一般", "9.999", "9.999"],
    ]
}


class BankRateTests(unittest.TestCase):
    def test_normalize_keeps_standard_deposits_and_rate_types(self):
        payload = rates.normalize(FIXTURE, "2026-09-09")
        rates.validate(payload)
        bank = payload["banks"][0]
        self.assertEqual(bank["bank_code"], "004")
        self.assertEqual(bank["bank_name"], "臺銀")
        self.assertEqual(bank["demand"]["demand_deposit"], 0.705)
        self.assertEqual(bank["time_deposit"]["1Y"], {"fixed": 1.7, "variable": 1.69})
        self.assertEqual(bank["time_savings_deposit"]["1Y"]["fixed"], 1.725)

    def test_source_failure_does_not_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bank_rates.json"
            original = '{"valid": true}\n'
            output.write_text(original, encoding="utf-8")
            with patch.object(rates, "OUTPUT", output), patch.object(rates, "fetch", side_effect=RuntimeError("offline")):
                self.assertEqual(rates.main(), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_current_json_schema_when_available(self):
        path = ROOT / "data" / "deposits" / "bank_rates.json"
        if not path.exists():
            self.skipTest("collector output is not present")
        payload = json.loads(path.read_text(encoding="utf-8"))
        rates.validate(payload)
        self.assertEqual(payload["provider"], "CBC")
        self.assertEqual(payload["unit"], "percent_per_annum")


class DepositPageSmokeTests(unittest.TestCase):
    def test_page_has_search_tables_rate_toggle_and_source(self):
        html = (ROOT / "deposits.html").read_text(encoding="utf-8")
        script = (ROOT / "deposits.js").read_text(encoding="utf-8")
        for value in ("銀行存款牌告利率比較", "bankSearch", "demandTable", "timeDepositTable", "timeSavingsTable", "fixed", "variable"):
            self.assertIn(value, html)
        self.assertIn("中央銀行牌告利率資訊網", html)
        self.assertIn('header.addEventListener("click"', script)
        self.assertIn('search.addEventListener("input"', script)
        self.assertIn("bank_rates.json", script)


if __name__ == "__main__":
    unittest.main()
