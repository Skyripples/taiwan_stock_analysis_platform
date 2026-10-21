"""Backfill PIT-safe TAIFEX TX institutional open-interest history locally."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from config import PROJECT_ROOT
from io_utils import atomic_write_json
from providers.taifex_provider import TaifexProvider
from trading_calendar import get_trading_days_in_range


UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")
VERSION = "v4.6-1.1"
ROOT = PROJECT_ROOT / "data_lake" / "futures_institutional"
CANONICAL_ROOT = ROOT / "daily_positions"
CHECKPOINT = ROOT / "_checkpoints" / "backfill.json"
SUMMARY = ROOT / "summary.json"
INVESTOR_TYPES = TaifexProvider.investor_names


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0)
    options = parser.parse_args()
    if options.start > options.end:
        parser.error("--start cannot exceed --end")
    if options.delay < 0:
        parser.error("--delay cannot be negative")
    return options


def institutional_available_at(trade_date: date) -> datetime:
    """Conservative availability: midnight Taipei on the next calendar day."""
    return datetime.combine(trade_date + timedelta(days=1), dt_time(), TAIPEI).astimezone(UTC)


def source_retention_start(today: date) -> date:
    """TAIFEX's public daily institutional query retains the previous three years."""
    try:
        return today.replace(year=today.year - 3)
    except ValueError:
        return today.replace(year=today.year - 3, day=28)


def aware_datetime(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Timestamp must be timezone-aware: {value}")
    return parsed.astimezone(UTC)


def load_checkpoint(path: Path | None = None) -> dict[str, Any]:
    path = path or CHECKPOINT
    if not path.exists():
        return {"version": VERSION, "dates": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"version": VERSION, "dates": {}}
    if not isinstance(value.get("dates"), dict):
        return {"version": VERSION, "dates": {}}
    if value.get("version") != VERSION:
        # v4.6-1.0 reused a session poisoned by an out-of-retention request,
        # so its official_empty statuses are not trustworthy. Preserve only
        # completed canonical successes during checkpoint migration.
        value["dates"] = {
            day: status for day, status in value["dates"].items()
            if isinstance(status, dict) and status.get("status") == "success"
        }
        value["version"] = VERSION
    return value


def save_checkpoint(checkpoint: dict[str, Any], path: Path | None = None) -> None:
    path = path or CHECKPOINT
    checkpoint["version"] = VERSION
    checkpoint["updated_at"] = datetime.now(UTC).isoformat()
    atomic_write_json(path, checkpoint)


def canonical_path(root: Path, year: int) -> Path:
    return root / f"partition_year={year}" / "part-00000.parquet"


def schema() -> Any:
    import pyarrow as pa

    utc = pa.timestamp("us", tz="UTC")
    return pa.schema([
        ("partition_year", pa.int64()), ("trade_date", pa.date32()), ("investor_type", pa.string()),
        ("long_open_interest", pa.int64()), ("short_open_interest", pa.int64()),
        ("net_open_interest", pa.int64()), ("long_contract_amount", pa.int64()),
        ("short_contract_amount", pa.int64()), ("net_contract_amount", pa.int64()),
        ("source", pa.string()), ("available_at", utc), ("fetched_at", utc),
    ])


def read_partition(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).read().to_pylist()


def existing_keys(root: Path | None = None) -> set[tuple[date, str]]:
    root = root or CANONICAL_ROOT
    keys: set[tuple[date, str]] = set()
    for path in root.glob("partition_year=*/part-00000.parquet"):
        for row in read_partition(path):
            keys.add((row["trade_date"], row["investor_type"]))
    return keys


def write_rows(rows: Iterable[dict[str, Any]], root: Path | None = None) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = root or CANONICAL_ROOT
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["trade_date"].year, []).append(row)
    affected = 0
    for year, additions in sorted(groups.items()):
        path = canonical_path(root, year)
        merged = {(row["trade_date"], row["investor_type"]): row for row in read_partition(path)}
        merged.update({(row["trade_date"], row["investor_type"]): row for row in additions})
        ordered = [merged[key] for key in sorted(merged)]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            table = pa.Table.from_pylist(ordered, schema=schema())
            pq.write_table(
                table, temporary, compression="zstd", compression_level=6,
                use_dictionary=["investor_type", "source"],
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        affected += len(additions)
    return affected


def canonical_rows(provider: TaifexProvider, raw_data: dict[str, Any]) -> list[dict[str, Any]]:
    fetched_at = aware_datetime(raw_data["fetched_at"])
    records = provider.normalize_investors(raw_data)
    if len(records) != len(INVESTOR_TYPES) or any(not provider.validate([record]) for record in records):
        raise ValueError("TAIFEX institutional records failed validation")
    trade_date = date.fromisoformat(raw_data["trade_date"])
    available_at = institutional_available_at(trade_date)
    if fetched_at < available_at:
        return []
    rows = []
    for record in records:
        rows.append({
            "partition_year": trade_date.year,
            "trade_date": trade_date,
            "investor_type": record["metadata"]["investor_type"],
            "long_open_interest": record["long_position"]["open_interest"],
            "short_open_interest": record["short_position"]["open_interest"],
            "net_open_interest": record["net_position"]["open_interest"],
            "long_contract_amount": record["long_position"]["contract_amount"],
            "short_contract_amount": record["short_position"]["contract_amount"],
            "net_contract_amount": record["net_position"]["contract_amount"],
            "source": provider.source_url,
            "available_at": available_at,
            "fetched_at": fetched_at,
        })
    return rows


def run(
    options: argparse.Namespace, *, provider: TaifexProvider | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    provider = provider or TaifexProvider()
    sessions, closed = get_trading_days_in_range(options.start, options.end)
    checkpoint = load_checkpoint() if options.resume else {"version": VERSION, "dates": {}}
    keys = existing_keys()
    required = set(INVESTOR_TYPES)
    summary: dict[str, Any] = {
        "version": VERSION, "start": options.start.isoformat(), "end": options.end.isoformat(),
        "official_sessions": len(sessions), "skipped_non_trading_days": len(closed),
        "network_requests": 0, "written_rows": 0, "skipped_existing": 0,
        "outside_source_retention": 0, "official_empty": 0,
        "skipped_current_session": 0, "failures": [],
    }
    requested = 0
    today_taipei = datetime.now(TAIPEI).date()
    retention_start = source_retention_start(today_taipei)
    for day in sessions:
        status = checkpoint.get("dates", {}).get(day.isoformat(), {}).get("status")
        present = {investor for existing_day, investor in keys if existing_day == day}
        if options.resume and (present == required or status in {"official_empty", "outside_source_retention"}):
            summary["skipped_existing"] += 1
            continue
        if day < retention_start:
            summary["outside_source_retention"] += 1
            checkpoint.setdefault("dates", {})[day.isoformat()] = {"status": "outside_source_retention"}
            save_checkpoint(checkpoint)
            continue
        if requested and options.delay:
            sleep(options.delay)
        requested += 1
        summary["network_requests"] += 1
        try:
            # A query outside TAIFEX retention can poison server-side state in
            # a reused cookie session. Exact historical requests are isolated.
            raw_data = provider.fetch_for_date(day)
            if raw_data is None:
                # Confirm an HTTP-200 empty result once with another fresh
                # session before recording a persistent official empty.
                summary["network_requests"] += 1
                raw_data = provider.fetch_for_date(day)
            if raw_data is None:
                if day < today_taipei:
                    summary["official_empty"] += 1
                    checkpoint.setdefault("dates", {})[day.isoformat()] = {"status": "official_empty"}
                    save_checkpoint(checkpoint)
                else:
                    summary["skipped_current_session"] += 1
                continue
            rows = canonical_rows(provider, raw_data)
            if not rows:
                summary["skipped_current_session"] += 1
                continue
            summary["written_rows"] += write_rows(rows)
            keys.update((row["trade_date"], row["investor_type"]) for row in rows)
            checkpoint.setdefault("dates", {})[day.isoformat()] = {
                "status": "success", "investor_types": sorted(required),
            }
            save_checkpoint(checkpoint)
        except Exception as exc:  # isolate a single source date; failed dates remain resumable
            summary["failures"].append({"trade_date": day.isoformat(), "error": str(exc)[:1000]})
    summary["completed_at"] = datetime.now(UTC).isoformat()
    atomic_write_json(SUMMARY, summary)
    return summary


def main() -> int:
    result = run(arguments())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
