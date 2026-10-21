from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import backfill_futures_institutional_positions as network_backfill
from research import import_taifex_institutional_csv as module


UTC = timezone.utc
HEADER = [
    "日期", "商品名稱", "身份別", "多方交易口數", "多方交易契約金額(千元)",
    "空方交易口數", "空方交易契約金額(千元)", "多空交易口數淨額",
    "多空交易契約金額淨額(千元)", "多方未平倉口數", "多方未平倉契約金額(千元)",
    "空方未平倉口數", "空方未平倉契約金額(千元)", "多空未平倉口數淨額",
    "多空未平倉契約金額淨額(千元)",
]


def source_row(day: str, product: str, investor: str, base: int) -> dict[str, str]:
    return {
        module.FIELDS["trade_date"]: day, module.FIELDS["product"]: product,
        module.FIELDS["investor"]: investor,
        module.FIELDS["long_trade_volume"]: str(base + 20),
        "多方交易契約金額(千元)": "1000",
        module.FIELDS["short_trade_volume"]: str(base + 10),
        "空方交易契約金額(千元)": "500",
        module.FIELDS["net_trade_volume"]: "10", "多空交易契約金額淨額(千元)": "500",
        module.FIELDS["long_open_interest"]: str(base + 100),
        module.FIELDS["long_contract_amount"]: "10000",
        module.FIELDS["short_open_interest"]: str(base + 40),
        module.FIELDS["short_contract_amount"]: "4000",
        module.FIELDS["net_open_interest"]: "60", module.FIELDS["net_contract_amount"]: "6000",
    }


def write_fixture(path: Path) -> None:
    rows = [
        source_row("2026/10/13", "臺股期貨", "自營商", 10),
        source_row("2026/10/13", "臺股期貨", "投信", 20),
        source_row("2026/10/13", "臺股期貨", "外資及陸資", 30),
        source_row("2026/10/13", "小型臺指期貨", "外資及陸資", 40),
    ]
    with path.open("w", encoding="big5", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    stamp = datetime(2026, 10, 14, 1, 0, tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))


class TaifexInstitutionalCsvImporterTests(unittest.TestCase):
    def test_big5_parser_filters_tx_and_normalizes_investors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "official.csv"
            write_fixture(path)
            rows, stats = module.read_csv(path)
        self.assertEqual(stats, {"raw_rows": 4, "products": 2, "tx_rows": 3})
        self.assertEqual({row["investor_type"] for row in rows}, {"外資", "投信", "自營商"})
        foreign = next(row for row in rows if row["investor_type"] == "外資")
        self.assertEqual(foreign["long_trade_volume"], 50)
        self.assertEqual(foreign["net_open_interest"], 60)
        self.assertEqual(foreign["available_at"], datetime(2026, 10, 13, 16, 0, tzinfo=UTC))
        self.assertEqual(foreign["fetched_at"], datetime(2026, 10, 14, 1, 0, tzinfo=UTC))

    def test_csv_rows_replace_legacy_network_rows_without_duplicates(self):
        day = date(2026, 10, 13)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "canonical"
            path = Path(directory) / "official.csv"
            write_fixture(path)
            legacy = {
                "partition_year": 2026, "trade_date": day, "investor_type": "外資",
                "long_open_interest": 1, "short_open_interest": 1, "net_open_interest": 0,
                "long_contract_amount": 1, "short_contract_amount": 1, "net_contract_amount": 0,
                "source": "network", "available_at": network_backfill.institutional_available_at(day),
                "fetched_at": datetime(2026, 10, 14, 2, 0, tzinfo=UTC),
            }
            network_backfill.write_rows([legacy], root)
            rows, _ = module.read_csv(path)
            first = module.write_rows(rows, root)
            second = module.write_rows(rows, root)
            stored = module.read_partition(module.partition_path(root, 2026))
        self.assertEqual(first, {"inserted": 2, "updated": 1, "partitions": 1})
        self.assertEqual(second, {"inserted": 0, "updated": 3, "partitions": 1})
        self.assertEqual(len(stored), 3)
        self.assertEqual(len({(row["trade_date"], row["investor_type"]) for row in stored}), 3)
        foreign = next(row for row in stored if row["investor_type"] == "外資")
        self.assertEqual(foreign["source"], module.SOURCE)
        self.assertEqual(foreign["long_trade_volume"], 50)

    def test_duplicate_canonical_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "official.csv"
            write_fixture(path)
            with path.open("a", encoding="big5", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=HEADER)
                writer.writerow(source_row("2026/10/13", "臺股期貨", "外資及陸資", 30))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                module.read_csv(path)


if __name__ == "__main__":
    unittest.main()
