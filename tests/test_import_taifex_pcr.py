from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import import_taifex_pcr as module


HEADER = list(module.FIELDS.values())


def raw_row(
    day: str, call_put: str, volume: int, open_interest: int,
    session: str, expiry: str, contract: str = "TXO",
) -> dict[str, str]:
    return {
        "交易日期": day, "契約": contract, "買賣權": call_put,
        "成交量": str(volume), "未沖銷契約數": str(open_interest),
        "交易時段": session, "契約到期日": expiry,
    }


def csv_bytes(rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=HEADER)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("big5")


class TaifexPcrImporterTests(unittest.TestCase):
    def test_2026_10_16_official_volume_and_oi_values(self):
        path = module.RAW_ROOT / "monthly" / "2026" / "taifex_options_2026-10.csv"
        with path.open("rb") as stream:
            daily = module.parse_csv(stream, str(path))
        values = daily[date(2026, 10, 16)]
        self.assertEqual(values["put_volume"], 396499)
        self.assertEqual(values["call_volume"], 446741)
        self.assertEqual(values["put_oi"], 55093)
        self.assertEqual(values["call_oi"], 81497)
        self.assertAlmostEqual(values["put_volume"] / values["call_volume"], 0.887536626367403)
        self.assertAlmostEqual(values["put_oi"] / values["call_oi"], 0.6760126139612501)

    def test_regular_future_expiry_oi_and_all_session_volume_rules(self):
        rows = [
            raw_row("2026/10/16", "賣權", 5, 100, "一般", "20261016"),
            raw_row("2026/10/16", "賣權", 13, 300, "一般", "20261021"),
            raw_row("2026/10/16", "賣權", 9, 999, "盤後", "20261021"),
            raw_row("2026/10/16", "買權", 7, 200, "一般", "20261021"),
            raw_row("2026/10/16", "買權", 11, 999, "盤後", "20261021"),
        ]
        values = module.parse_csv(io.BytesIO(csv_bytes(rows)), "fixture.csv")[date(2026, 10, 16)]
        self.assertEqual(values["put_volume"], 27)  # includes regular, expiry-day, and after-hours
        self.assertEqual(values["call_volume"], 18)
        self.assertEqual(values["put_oi"], 300)  # expiry-day and after-hours are excluded
        self.assertEqual(values["call_oi"], 200)

    def test_legacy_csv_without_expiry_metadata_keeps_oi_null(self):
        headers = ["交易日期", "契約", "買賣權", "成交量"]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerows([
            {"交易日期": "2023/01/03", "契約": "TXO", "買賣權": "賣權", "成交量": "10"},
            {"交易日期": "2023/01/03", "契約": "TXO", "買賣權": "買權", "成交量": "20"},
        ])
        values = module.parse_csv(io.BytesIO(stream.getvalue().encode("big5")), "legacy.csv")[date(2023, 1, 3)]
        self.assertFalse(values["oi_available"])
        self.assertIsNone(values["put_oi"])
        self.assertIsNone(values["call_oi"])

    def test_zip_and_csv_sources_are_read_without_filename_date_inference(self):
        rows = [raw_row("2026/10/16", "賣權", 3, 4, "一般", "20261021"),
                raw_row("2026/10/16", "買權", 6, 8, "一般", "20261021")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / "opaque.zip", "w") as archive:
                archive.writestr("opaque.csv", csv_bytes(rows))
            output, stats = module.read_sources(root)
        self.assertEqual(stats, {"source_csv_files": 1, "zip_csv_entries": 1, "rows": 1,
                                 "oi_available_rows": 1})
        self.assertEqual(output[0]["trade_date"], date(2026, 10, 16))
        self.assertEqual(output[0]["put_volume"], 3)

    def test_duplicate_trade_date_across_sources_is_rejected(self):
        rows = [raw_row("2026/10/16", "賣權", 3, 4, "一般", "20261021"),
                raw_row("2026/10/16", "買權", 6, 8, "一般", "20261021")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.csv").write_bytes(csv_bytes(rows))
            (root / "two.csv").write_bytes(csv_bytes(rows))
            with self.assertRaisesRegex(ValueError, "Duplicate TXO trade_date"):
                module.read_sources(root)

    def test_partition_write_is_idempotent_with_unique_trade_date(self):
        rows = [
            {"partition_year": 2026, "trade_date": date(2026, 10, 16),
             "put_volume": 3, "call_volume": 6, "volume_pcr": .5,
             "put_oi": 4, "call_oi": 8, "oi_pcr": .5, "oi_available": True, "source": module.SOURCE,
             "available_at": module.institutional_available_at(date(2026, 10, 16))},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = module.write_rows(rows, root)
            second = module.write_rows(rows, root)
            stored = module.read_partition(module.partition_path(root, 2026))
        self.assertEqual(first, {"inserted": 1, "updated": 0, "partitions": 1})
        self.assertEqual(second, {"inserted": 0, "updated": 1, "partitions": 1})
        self.assertEqual(len(stored), 1)


if __name__ == "__main__":
    unittest.main()
