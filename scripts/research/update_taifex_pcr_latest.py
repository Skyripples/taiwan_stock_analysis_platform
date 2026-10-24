"""Ingest the latest official TAIFEX TXO Put/Call Ratio observation locally."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import requests

# Permit the documented ``python scripts/research/...`` invocation as well as
# package imports used by tests.
SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from research.backfill_futures_institutional_positions import institutional_available_at
from research.import_taifex_pcr import OUTPUT_ROOT, write_rows


SOURCE_URL = "https://openapi.taifex.com.tw/v1/PutCallRatio"
SOURCE = "TAIFEX OpenAPI PutCallRatio"
FIELDS = {
    "trade_date": "日期",
    "put_volume": "賣權成交量",
    "call_volume": "買權成交量",
    "volume_pcr": "買賣權成交量比率%",
    "put_oi": "賣權未平倉量",
    "call_oi": "買權未平倉量",
    "oi_pcr": "買賣權未平倉量比率%",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="Official trade date to select; defaults to latest")
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    return parser.parse_args()


def parse_trade_date(value: str) -> date:
    value = value.strip()
    for pattern in ("%Y%m%d", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid TAIFEX Put/Call trade date: {value!r}")


def integer(value: str | None) -> int | None:
    text = (value or "").replace(",", "").strip()
    return None if text in {"", "-", "--", "---"} else int(text)


def ratio(value: str | None) -> float | None:
    """Convert TAIFEX's published percentage (e.g. 88.75) to decimal."""
    text = (value or "").replace("%", "").replace(",", "").strip()
    return None if text in {"", "-", "--", "---"} else float(text) / 100


def canonical_row(raw: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    """Map either legacy CSV headers or the current OpenAPI JSON headers."""
    trade_date = parse_trade_date(str(raw[fields["trade_date"]]))
    put_oi = integer(raw.get(fields["put_oi"]))
    call_oi = integer(raw.get(fields["call_oi"]))
    oi_pcr = ratio(raw.get(fields["oi_pcr"]))
    oi_available = put_oi is not None and call_oi is not None and oi_pcr is not None
    return {
        "partition_year": trade_date.year,
        "trade_date": trade_date,
        "put_volume": integer(raw.get(fields["put_volume"])),
        "call_volume": integer(raw.get(fields["call_volume"])),
        "volume_pcr": ratio(raw.get(fields["volume_pcr"])),
        "put_oi": put_oi if oi_available else None,
        "call_oi": call_oi if oi_available else None,
        "oi_pcr": oi_pcr if oi_available else None,
        "oi_available": oi_available,
        "source": SOURCE,
        "available_at": institutional_available_at(trade_date),
    }


OPENAPI_FIELDS = {
    "trade_date": "Date", "put_volume": "PutVolume", "call_volume": "CallVolume",
    "volume_pcr": "PutCallVolumeRatio%", "put_oi": "PutOI", "call_oi": "CallOI",
    "oi_pcr": "PutCallOIRatio%",
}


def parse_csv(payload: bytes) -> list[dict[str, Any]]:
    """Parse legacy CSV or TAIFEX's current JSON OpenAPI response."""
    text = payload.decode("utf-8-sig")
    if text.lstrip().startswith("["):
        raw_rows = json.loads(text)
        if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
            raise ValueError("TAIFEX Put/Call JSON response must be an array of objects")
        headers = set().union(*(row.keys() for row in raw_rows)) if raw_rows else set()
        missing = set(OPENAPI_FIELDS.values()) - headers
        if missing:
            raise ValueError(f"TAIFEX Put/Call JSON response missing columns: {sorted(missing)}")
        source_rows, fields = raw_rows, OPENAPI_FIELDS
    else:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = set(reader.fieldnames or [])
        missing = set(FIELDS.values()) - headers
        if missing:
            raise ValueError(f"TAIFEX Put/Call CSV response missing columns: {sorted(missing)}")
        source_rows, fields = list(reader), FIELDS

    rows: list[dict[str, Any]] = []
    seen: set[date] = set()
    for raw in source_rows:
        row = canonical_row(raw, fields)
        trade_date = row["trade_date"]
        if trade_date in seen:
            raise ValueError(f"Duplicate TAIFEX Put/Call trade date: {trade_date}")
        seen.add(trade_date)
        rows.append(row)
    return rows


def fetch_recent() -> bytes:
    """Make exactly one request; TAIFEX itself bounds this endpoint to recent data."""
    response = requests.get(SOURCE_URL, headers={"Accept": "text/csv"}, timeout=(10, 30))
    response.raise_for_status()
    return response.content


def select_observation(rows: list[dict[str, Any]], requested: date | None) -> dict[str, Any] | None:
    if not rows:
        return None
    if requested is not None:
        return next((row for row in rows if row["trade_date"] == requested), None)
    return max(rows, key=lambda row: row["trade_date"])


def run(options: argparse.Namespace, fetch: Callable[[], bytes] = fetch_recent) -> dict[str, Any]:
    selected = select_observation(parse_csv(fetch()), options.date)
    if selected is None:
        return {
            "source": SOURCE_URL,
            "requested_date": options.date.isoformat() if options.date else "latest",
            "observation": None,
            "written": False,
            "reason": "no_observation",
        }
    write_stats = write_rows([selected], options.output)
    return {
        "source": SOURCE_URL,
        "requested_date": options.date.isoformat() if options.date else "latest",
        "observation": selected["trade_date"].isoformat(),
        "written": True,
        **write_stats,
    }


def main() -> int:
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
