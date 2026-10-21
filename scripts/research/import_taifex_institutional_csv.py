"""Import an official TAIFEX Big5 institutional CSV into the local canonical lake."""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import PROJECT_ROOT
from research.backfill_futures_institutional_positions import institutional_available_at


UTC = timezone.utc
VERSION = "v4.6-1.0"
RAW_ROOT = PROJECT_ROOT / "data_lake" / "raw" / "taifex_institutional"
DEFAULT_INPUT = RAW_ROOT / "taifex_institutional_2023-10-20_2026-10-20.csv"
CANONICAL_ROOT = PROJECT_ROOT / "data_lake" / "futures_institutional" / "daily_positions"
TARGET_PRODUCT = "臺股期貨"
SOURCE = "TAIFEX official institutional batch CSV"
INVESTOR_TYPES = {"外資及陸資": "外資", "外資": "外資", "投信": "投信", "自營商": "自營商"}
FIELDS = {
    "trade_date": "日期", "product": "商品名稱", "investor": "身份別",
    "long_trade_volume": "多方交易口數", "short_trade_volume": "空方交易口數",
    "net_trade_volume": "多空交易口數淨額",
    "long_open_interest": "多方未平倉口數", "long_contract_amount": "多方未平倉契約金額(千元)",
    "short_open_interest": "空方未平倉口數", "short_contract_amount": "空方未平倉契約金額(千元)",
    "net_open_interest": "多空未平倉口數淨額", "net_contract_amount": "多空未平倉契約金額淨額(千元)",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=CANONICAL_ROOT)
    return parser.parse_args()


def resolve_input(path: Path) -> Path:
    if path.exists():
        return path
    if path == DEFAULT_INPUT:
        matches = sorted(RAW_ROOT.glob("*taifex_institutional_2023-10-20_2026-10-20.csv"))
        if len(matches) == 1:
            return matches[0]
    raise FileNotFoundError(path)


def normalized_label(value: Any) -> str:
    return "".join(str(value).split()).replace("台股期貨", "臺股期貨")


def integer(value: Any) -> int:
    text = str(value).replace(",", "").strip()
    if not text:
        raise ValueError("TAIFEX CSV numeric field is empty")
    return int(text)


def artifact_timestamp(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def read_csv(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    fetched_at = artifact_timestamp(path)
    rows: list[dict[str, Any]] = []
    raw_rows = 0
    products: set[str] = set()
    with path.open("r", encoding="big5", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = set(FIELDS.values()) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"TAIFEX CSV missing columns: {sorted(missing)}")
        for source_row in reader:
            raw_rows += 1
            product = normalized_label(source_row[FIELDS["product"]])
            products.add(product)
            if product != TARGET_PRODUCT:
                continue
            raw_investor = normalized_label(source_row[FIELDS["investor"]])
            investor = INVESTOR_TYPES.get(raw_investor)
            if investor is None:
                raise ValueError(f"Unsupported TX investor type: {raw_investor}")
            trade_date = datetime.strptime(source_row[FIELDS["trade_date"]].strip(), "%Y/%m/%d").date()
            row = {
                "partition_year": trade_date.year, "trade_date": trade_date, "investor_type": investor,
                "long_open_interest": integer(source_row[FIELDS["long_open_interest"]]),
                "short_open_interest": integer(source_row[FIELDS["short_open_interest"]]),
                "net_open_interest": integer(source_row[FIELDS["net_open_interest"]]),
                "long_contract_amount": integer(source_row[FIELDS["long_contract_amount"]]),
                "short_contract_amount": integer(source_row[FIELDS["short_contract_amount"]]),
                "net_contract_amount": integer(source_row[FIELDS["net_contract_amount"]]),
                "long_trade_volume": integer(source_row[FIELDS["long_trade_volume"]]),
                "short_trade_volume": integer(source_row[FIELDS["short_trade_volume"]]),
                "net_trade_volume": integer(source_row[FIELDS["net_trade_volume"]]),
                "source": SOURCE, "available_at": institutional_available_at(trade_date),
                "fetched_at": fetched_at,
            }
            if row["long_open_interest"] - row["short_open_interest"] != row["net_open_interest"]:
                raise ValueError(f"TX open-interest net mismatch: {trade_date} {investor}")
            if row["long_trade_volume"] - row["short_trade_volume"] != row["net_trade_volume"]:
                raise ValueError(f"TX trade-volume net mismatch: {trade_date} {investor}")
            rows.append(row)
    keys = [(row["trade_date"], row["investor_type"]) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("TAIFEX CSV contains duplicate TX trade_date + investor_type keys")
    return rows, {"raw_rows": raw_rows, "products": len(products), "tx_rows": len(rows)}


def schema() -> Any:
    import pyarrow as pa

    utc = pa.timestamp("us", tz="UTC")
    return pa.schema([
        ("partition_year", pa.int64()), ("trade_date", pa.date32()), ("investor_type", pa.string()),
        ("long_open_interest", pa.int64()), ("short_open_interest", pa.int64()),
        ("net_open_interest", pa.int64()), ("long_contract_amount", pa.int64()),
        ("short_contract_amount", pa.int64()), ("net_contract_amount", pa.int64()),
        ("long_trade_volume", pa.int64()), ("short_trade_volume", pa.int64()),
        ("net_trade_volume", pa.int64()), ("source", pa.string()),
        ("available_at", utc), ("fetched_at", utc),
    ])


def partition_path(root: Path, year: int) -> Path:
    return root / f"partition_year={year}" / "part-00000.parquet"


def read_partition(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).read().to_pylist()


def write_rows(rows: Iterable[dict[str, Any]], root: Path) -> dict[str, int]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["trade_date"].year, []).append(row)
    inserted = updated = 0
    for year, additions in sorted(groups.items()):
        path = partition_path(root, year)
        existing = {(row["trade_date"], row["investor_type"]): row for row in read_partition(path)}
        for row in additions:
            key = (row["trade_date"], row["investor_type"])
            if key in existing:
                updated += 1
            else:
                inserted += 1
            existing[key] = row
        ordered = [existing[key] for key in sorted(existing)]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            table = pa.Table.from_pylist(ordered, schema=schema())
            pq.write_table(table, temporary, compression="zstd", compression_level=6,
                           use_dictionary=["investor_type", "source"])
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"inserted": inserted, "updated": updated, "partitions": len(groups)}


def run(options: argparse.Namespace) -> dict[str, Any]:
    input_path = resolve_input(options.input)
    rows, source_stats = read_csv(input_path)
    write_stats = write_rows(rows, options.output)
    return {"version": VERSION, "input": str(input_path), "output": str(options.output),
            **source_stats, **write_stats, "canonical_rows_processed": len(rows)}


def main() -> int:
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
