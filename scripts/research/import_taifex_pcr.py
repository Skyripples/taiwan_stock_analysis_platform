"""Build local daily TXO Put/Call Ratio observations from official TAIFEX files."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from config import PROJECT_ROOT
from research.backfill_futures_institutional_positions import institutional_available_at


VERSION = "v4.6-1.0"
RAW_ROOT = PROJECT_ROOT / "data_lake" / "raw" / "taifex_pcr"
OUTPUT_ROOT = PROJECT_ROOT / "data_lake" / "futures_options" / "daily_pcr"
SOURCE = "TAIFEX official options daily files"
TXO = "TXO"
PUT = "賣權"
CALL = "買權"
REGULAR = "一般"
FIELDS = {
    "trade_date": "交易日期", "contract": "契約", "call_put": "買賣權",
    "volume": "成交量", "open_interest": "未沖銷契約數",
    "session": "交易時段", "expiry_date": "契約到期日",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=RAW_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    return parser.parse_args()


def number(value: Any) -> int:
    text = str(value or "").replace(",", "").strip()
    return 0 if text in {"", "-", "--", "---"} else int(text)


def parse_trade_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y/%m/%d").date()


def parse_expiry_date(value: str) -> date | None:
    value = value.strip()
    if len(value) != 8 or not value.isdigit():
        return None
    return datetime.strptime(value, "%Y%m%d").date()


def parse_csv(stream: BinaryIO, source_name: str) -> dict[date, dict[str, Any]]:
    """Parse one Big5/CP950 official CSV without relying on its filename."""
    payload = stream.read()
    last_error: UnicodeDecodeError | None = None
    for encoding in ("big5", "cp950"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        raise ValueError(f"Cannot decode TAIFEX CSV {source_name}: {last_error}")

    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = set(reader.fieldnames or [])
    volume_columns = {FIELDS[key] for key in ("trade_date", "contract", "call_put", "volume")}
    missing = volume_columns - headers
    if missing:
        raise ValueError(f"TAIFEX CSV {source_name} missing volume columns: {sorted(missing)}")
    oi_available = {FIELDS[key] for key in ("open_interest", "session", "expiry_date")} <= headers
    output: dict[date, dict[str, Any]] = defaultdict(lambda: {
        "put_volume": 0, "call_volume": 0,
        "put_oi": 0 if oi_available else None, "call_oi": 0 if oi_available else None,
        "oi_available": oi_available,
    })
    for row in reader:
        if (row.get(FIELDS["contract"]) or "").strip() != TXO:
            continue
        call_put = (row.get(FIELDS["call_put"]) or "").strip()
        if call_put not in {PUT, CALL}:
            continue
        trade_date = parse_trade_date(row[FIELDS["trade_date"]])
        values = output[trade_date]
        volume = number(row.get(FIELDS["volume"]))
        if call_put == PUT:
            values["put_volume"] += volume
        else:
            values["call_volume"] += volume

        if not oi_available:
            continue
        expiry_date = parse_expiry_date(row.get(FIELDS["expiry_date"]) or "")
        if (row.get(FIELDS["session"]) or "").strip() != REGULAR or expiry_date is None or expiry_date <= trade_date:
            continue
        open_interest = number(row.get(FIELDS["open_interest"]))
        if call_put == PUT:
            values["put_oi"] += open_interest
        else:
            values["call_oi"] += open_interest
    return dict(output)


def source_entries(root: Path) -> Iterable[tuple[str, BinaryIO]]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".csv":
            with path.open("rb") as stream:
                yield str(path.relative_to(root)), io.BytesIO(stream.read())
        elif path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                invalid = archive.testzip()
                if invalid:
                    raise ValueError(f"Corrupt TAIFEX ZIP member: {path} {invalid}")
                for member in archive.infolist():
                    if member.is_dir() or not member.filename.lower().endswith(".csv"):
                        continue
                    with archive.open(member) as stream:
                        yield f"{path.relative_to(root)}:{member.filename}", io.BytesIO(stream.read())


def read_sources(root: Path = RAW_ROOT) -> tuple[list[dict[str, Any]], dict[str, int]]:
    by_date: dict[date, dict[str, Any]] = {}
    source_for_date: dict[date, str] = {}
    files = zip_entries = 0
    for source_name, stream in source_entries(root):
        files += 1
        if ":" in source_name:
            zip_entries += 1
        daily = parse_csv(stream, source_name)
        for trade_date, values in daily.items():
            if trade_date in by_date:
                raise ValueError(
                    f"Duplicate TXO trade_date across raw sources: {trade_date} "
                    f"({source_for_date[trade_date]} and {source_name})"
                )
            by_date[trade_date] = values
            source_for_date[trade_date] = source_name
    rows = []
    for trade_date, values in sorted(by_date.items()):
        rows.append({
            "partition_year": trade_date.year, "trade_date": trade_date,
            **values,
            "volume_pcr": values["put_volume"] / values["call_volume"] if values["call_volume"] else None,
            "oi_pcr": values["put_oi"] / values["call_oi"] if values["oi_available"] and values["call_oi"] else None,
            "source": SOURCE, "available_at": institutional_available_at(trade_date),
        })
    return rows, {"source_csv_files": files, "zip_csv_entries": zip_entries, "rows": len(rows),
                  "oi_available_rows": sum(row["oi_available"] for row in rows)}


def schema() -> Any:
    import pyarrow as pa

    return pa.schema([
        ("partition_year", pa.int64()), ("trade_date", pa.date32()),
        ("put_volume", pa.int64()), ("call_volume", pa.int64()), ("volume_pcr", pa.float64()),
        ("put_oi", pa.int64()), ("call_oi", pa.int64()), ("oi_pcr", pa.float64()), ("oi_available", pa.bool_()),
        ("source", pa.string()), ("available_at", pa.timestamp("us", tz="UTC")),
    ])


def partition_path(root: Path, year: int) -> Path:
    return root / f"partition_year={year}" / "part-00000.parquet"


def read_partition(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).read().to_pylist()


def write_rows(rows: Iterable[dict[str, Any]], root: Path = OUTPUT_ROOT) -> dict[str, int]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["trade_date"].year].append(row)
    inserted = updated = 0
    for year, additions in sorted(groups.items()):
        path = partition_path(root, year)
        existing = {row["trade_date"]: row for row in read_partition(path)}
        for row in additions:
            if row["trade_date"] in existing:
                updated += 1
            else:
                inserted += 1
            existing[row["trade_date"]] = row
        ordered = [existing[key] for key in sorted(existing)]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            pq.write_table(
                pa.Table.from_pylist(ordered, schema=schema()), temporary,
                compression="zstd", compression_level=6, use_dictionary=["source"],
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"inserted": inserted, "updated": updated, "partitions": len(groups)}


def run(options: argparse.Namespace) -> dict[str, Any]:
    rows, read_stats = read_sources(options.input)
    write_stats = write_rows(rows, options.output)
    return {"version": VERSION, "input": str(options.input), "output": str(options.output),
            **read_stats, **write_stats}


def main() -> int:
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
