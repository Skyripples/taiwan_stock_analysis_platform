"""Build one immutable, provenance-backed DAY 08:15 TX Core snapshot.

The underlying TX Core calculation is reused from the B4 snapshot builder,
but every selected source is rechecked against the stricter DAY 08:15 cutoff.
No historical dataset row is used as a live snapshot substitute.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import PROJECT_ROOT
from research import build_futures_b4_live_snapshot as b4_snapshot
from research import build_futures_day_dataset as day
from research import run_futures_day_forward_prediction as forward


VERSION = "v5.day-live-snapshot-1.0"
TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "day_0815_live_snapshots"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prediction-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--tx-root", type=Path, default=b4_snapshot.TX_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--mode", choices=("live", "replay"), default="live")
    parser.add_argument("--dry-run", action="store_true", help="Build and validate only; do not publish a snapshot.")
    return parser.parse_args()


def _aware(value: Any, field: str) -> datetime:
    return b4_snapshot._aware(value, field)


def unavailable_snapshot(prediction_date: date, generated_at: datetime, mode: str, reason: str,
                         sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "snapshot_version": VERSION, "status": "unavailable", "unavailable_reason": reason,
        "mode": mode, "prediction_date": prediction_date.isoformat(),
        "prediction_at": day.prediction_timestamp(prediction_date).isoformat(),
        "generated_at": generated_at.isoformat(), "sources": sources or [],
        "feature_columns": list(day.FEATURE_COLUMNS),
    }


def build_snapshot(prediction_date: date, rows: list[dict[str, Any]], *, generated_at: datetime,
                   mode: str) -> dict[str, Any]:
    """Reuse the Core calculator, then reject any source late for DAY 08:15."""
    cutoff = day.prediction_timestamp(prediction_date)
    underlying = b4_snapshot.build_snapshot(prediction_date, rows, generated_at=generated_at, mode=mode)
    sources = list(underlying.get("sources") or [])
    if underlying.get("status") != "ready":
        return unavailable_snapshot(prediction_date, generated_at, mode,
                                    str(underlying.get("unavailable_reason") or "source_unavailable"), sources)
    try:
        available = [_aware(source["available_at"], "source available_at") for source in sources]
        fetched = [_aware(source["fetched_at"], "source fetched_at") for source in sources]
    except (KeyError, TypeError, ValueError) as error:
        return unavailable_snapshot(prediction_date, generated_at, mode, f"missing_or_invalid_source_timestamp:{error}", sources)
    if not sources:
        return unavailable_snapshot(prediction_date, generated_at, mode, "missing_source_provenance")
    if max(available) > cutoff:
        return unavailable_snapshot(prediction_date, generated_at, mode, "source_available_after_day_0815_cutoff", sources)
    if max(fetched) > cutoff:
        return unavailable_snapshot(prediction_date, generated_at, mode, "source_fetched_after_day_0815_cutoff", sources)
    snapshot = dict(underlying)
    snapshot.update({
        "snapshot_version": VERSION,
        "prediction_at": cutoff.isoformat(),
        "max_feature_available_at": max(available).isoformat(),
        "source_fetched_at": max(fetched).isoformat(),
        "feature_available_at": {name: max(available).isoformat() for name in day.FEATURE_COLUMNS},
        "day_cutoff_evidence": "all selected source available_at and fetched_at <= 08:15 Asia/Taipei",
    })
    return snapshot


def inspect_existing_snapshot(path: Path, prediction_date: date, mode: str) -> dict[str, Any]:
    """Validate an immutable snapshot without changing it or its mtime."""
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"existing_snapshot_invalid_json:{error}") from error
    if payload.get("prediction_date") != prediction_date.isoformat():
        raise ValueError("existing_snapshot_prediction_date_mismatch")
    normalized = forward.normalize_snapshot(payload)
    status, reason = forward.snapshot_status(normalized, mode)
    return {
        "snapshot": str(path), "action": "reused_existing", "status": status, "reason": reason,
        "prediction_at": normalized["prediction_at"].isoformat(),
        "generated_at": normalized["generated_at"].isoformat(),
        "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        "content_hash": hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "read_only": True,
    }


def run(options: argparse.Namespace, *, now: datetime | None = None) -> dict[str, Any]:
    destination = options.output_root / f"{options.prediction_date.isoformat()}.json"
    if destination.exists():
        return inspect_existing_snapshot(destination, options.prediction_date, options.mode)
    generated = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    snapshot = build_snapshot(options.prediction_date, b4_snapshot.read_tx_rows(options.tx_root),
                              generated_at=generated, mode=options.mode)
    # ``generated_at`` is completion time, never the scheduler start time.
    completed = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    snapshot["generated_at"] = completed.isoformat()
    if options.dry_run:
        return {"action": "dry_run", "status": snapshot["status"], "reason": snapshot.get("unavailable_reason"),
                "prediction_at": snapshot["prediction_at"], "generated_at": completed.isoformat(), "dry_run": True}
    path = b4_snapshot.write_snapshot(snapshot, options.output_root)
    return {"snapshot": str(path), "action": "created", "status": snapshot["status"], "reason": snapshot.get("unavailable_reason"),
            "generated_at": snapshot["generated_at"]}


if __name__ == "__main__":
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
