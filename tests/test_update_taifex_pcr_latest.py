from __future__ import annotations

import sys
import tempfile
import unittest
import json
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import import_taifex_pcr as historical
from research import update_taifex_pcr_latest as module


def payload(*rows: str) -> bytes:
    header = "日期,賣權成交量,買權成交量,買賣權成交量比率%,賣權未平倉量,買權未平倉量,買賣權未沖銷量比率%"
    # The production header calls the final field 未平倉量比率; make fixtures use it exactly.
    header = header.replace("未沖銷量", "未平倉量")
    return (header + "\n" + "\n".join(rows) + "\n").encode("utf-8-sig")


class TaifexPcrLatestTests(unittest.TestCase):
    def test_current_openapi_json_maps_to_the_same_canonical_schema(self):
        raw = json.dumps([{
            "Date": "20261022", "PutVolume": "172113", "CallVolume": "199654",
            "PutCallVolumeRatio%": "86.21", "PutOI": "64809", "CallOI": "97930",
            "PutCallOIRatio%": "66.18",
        }]).encode("utf-8")
        row = module.parse_csv(raw)[0]
        self.assertEqual(row["trade_date"], date(2026, 10, 22))
        self.assertEqual(row["put_volume"], 172113)
        self.assertAlmostEqual(row["volume_pcr"], 0.8621)
        self.assertAlmostEqual(row["oi_pcr"], 0.6618)
    def test_official_percentages_map_to_decimal_canonical_values(self):
        row = module.parse_csv(payload("20261022,172113,199654,86.21,64809,97930,66.18"))[0]
        self.assertEqual(row["trade_date"], date(2026, 10, 22))
        self.assertEqual(row["put_volume"], 172113)
        self.assertEqual(row["call_volume"], 199654)
        self.assertAlmostEqual(row["volume_pcr"], 0.8621)
        self.assertEqual(row["put_oi"], 64809)
        self.assertEqual(row["call_oi"], 97930)
        self.assertAlmostEqual(row["oi_pcr"], 0.6618)
        self.assertTrue(row["oi_available"])
        self.assertEqual(row["available_at"], module.institutional_available_at(date(2026, 10, 22)))

    def test_missing_official_oi_stays_null(self):
        row = module.parse_csv(payload("20260102,100,200,50.00,,,,"))[0]
        self.assertEqual(row["volume_pcr"], 0.5)
        self.assertFalse(row["oi_available"])
        self.assertIsNone(row["put_oi"])
        self.assertIsNone(row["call_oi"])
        self.assertIsNone(row["oi_pcr"])

    def test_existing_partition_merges_idempotently_without_damaging_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            historical.write_rows([{
                "partition_year": 2026, "trade_date": date(2026, 10, 21), "put_volume": 1,
                "call_volume": 2, "volume_pcr": 0.5, "put_oi": 3, "call_oi": 4,
                "oi_pcr": 0.75, "oi_available": True, "source": "history",
                "available_at": module.institutional_available_at(date(2026, 10, 21)),
            }], root)
            options = type("Options", (), {"date": None, "output": root})()
            first = module.run(options, fetch=lambda: payload("20261022,100,200,50.00,30,40,75.00"))
            second = module.run(options, fetch=lambda: payload("20261022,101,200,50.50,30,40,75.00"))
            stored = historical.read_partition(root / "partition_year=2026" / "part-00000.parquet")
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["updated"], 1)
            self.assertEqual([item["trade_date"] for item in stored], [date(2026, 10, 21), date(2026, 10, 22)])
            self.assertEqual(next(item for item in stored if item["trade_date"] == date(2026, 10, 22))["put_volume"], 101)

    def test_requested_missing_date_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = type("Options", (), {"date": date(2026, 10, 23), "output": root})()
            result = module.run(options, fetch=lambda: payload("20261022,100,200,50.00,30,40,75.00"))
            self.assertFalse(result["written"])
            self.assertEqual(result["reason"], "no_observation")
            self.assertFalse(any(root.rglob("*.parquet")))


if __name__ == "__main__":
    unittest.main()
