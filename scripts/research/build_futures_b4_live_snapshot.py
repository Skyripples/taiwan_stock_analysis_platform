"""Build one immutable, provenance-backed B4 TX Core feature snapshot.

The builder reads only local TX daily staging.  It deliberately does not read
the B4 label dataset and never downloads data.  A missing prior TX source or
missing acquisition timestamp yields an ``unavailable`` snapshot instead of a
substitute from a later research build.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import PROJECT_ROOT
from research import build_futures_daily_pit_alignment as pit
from research.futures_binary.features import feature_names
from taifex_trading_calendar import get_taifex_trading_days_in_range, is_taifex_trading_day


VERSION = "v5.b4-live-snapshot-1.0"
TAIPEI = ZoneInfo("Asia/Taipei")
FEATURE_COLUMNS = feature_names(("tx_core",))
TX_ROOT = PROJECT_ROOT / "data_lake" / "staging" / "futures_daily" / "product=TX"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "b4_live_snapshots"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prediction-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--tx-root", type=Path, default=TX_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--mode", choices=("live", "replay"), default="live")
    return parser.parse_args()


def _aware(value: Any, field: str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_tx_rows(root: Path) -> list[dict[str, Any]]:
    """Read local regular front rows and attach immutable file provenance."""
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("year=*/daily.jsonl.gz")):
        source_hash = _file_hash(path)
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for line in source:
                raw = json.loads(line)
                if raw.get("product") != "TX" or raw.get("session") != "regular" or raw.get("contract_rank") != "front":
                    continue
                raw["trade_date"] = date.fromisoformat(str(raw["trade_date"])[:10])
                raw["_source_path"] = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/") if path.is_relative_to(PROJECT_ROOT) else str(path)
                raw["_source_sha256"] = source_hash
                rows.append(raw)
    return rows


def _source_evidence(row: dict[str, Any]) -> dict[str, str]:
    available = _aware(row.get("available_at"), "available_at")
    fetched = _aware(row.get("fetched_at"), "fetched_at")
    return {
        "source_date": row["trade_date"].isoformat(), "contract": str(row["contract"]),
        "path": row["_source_path"], "sha256": row["_source_sha256"],
        "available_at": available.astimezone(timezone.utc).isoformat(),
        "fetched_at": fetched.astimezone(timezone.utc).isoformat(),
    }


def unavailable_snapshot(day: date, generated_at: datetime, mode: str, reason: str, *, sources: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "snapshot_version": VERSION, "status": "unavailable", "unavailable_reason": reason,
        "mode": mode, "prediction_date": day.isoformat(), "prediction_at": pit.prediction_timestamp(day).isoformat(),
        "generated_at": generated_at.isoformat(), "sources": sources or [],
        "feature_columns": list(FEATURE_COLUMNS),
    }


def build_snapshot(day: date, rows: list[dict[str, Any]], *, generated_at: datetime, mode: str) -> dict[str, Any]:
    cutoff = pit.prediction_timestamp(day)
    if not is_taifex_trading_day(day):
        return unavailable_snapshot(day, generated_at, mode, "not_a_taifex_prediction_session")
    # Five-session volatility needs at most six preceding sessions; a bounded
    # calendar lookback avoids demanding irrelevant historical calendar years.
    sessions, _ = get_taifex_trading_days_in_range(day - timedelta(days=45), day)
    if day not in sessions:
        return unavailable_snapshot(day, generated_at, mode, "prediction_session_not_covered_by_calendar")
    index = sessions.index(day)
    if index < 6:
        return unavailable_snapshot(day, generated_at, mode, "insufficient_prior_taifex_sessions")
    by_date: dict[date, dict[str, Any]] = {}
    for row in rows:
        source_day = row["trade_date"]
        if source_day in by_date:
            raise ValueError(f"Duplicate TX front regular row: {source_day}")
        by_date[source_day] = row
    anchor_day = sessions[index - 1]
    anchor = by_date.get(anchor_day)
    if anchor is None:
        return unavailable_snapshot(day, generated_at, mode, f"missing_tx_front_regular:{anchor_day.isoformat()}")
    prior = by_date.get(sessions[index - 2])
    evidence_rows = [anchor]
    if prior is not None:
        evidence_rows.append(prior)
    # Add all source rows actually needed for a non-null five-session volatility.
    volatility_rows = [by_date.get(sessions[position]) for position in range(index - 6, index)]
    if all(volatility_rows):
        evidence_rows.extend(volatility_rows)
    unique_evidence = {(
        item["trade_date"], item["contract"], item["_source_path"], item["_source_sha256"],
    ): item for item in evidence_rows}.values()
    try:
        evidence = [_source_evidence(item) for item in unique_evidence]
    except (TypeError, ValueError) as error:
        return unavailable_snapshot(day, generated_at, mode, f"missing_or_invalid_source_timestamp:{error}")
    availabilities = [_aware(item["available_at"], "available_at") for item in evidence_rows]
    fetched = [_aware(item["fetched_at"], "fetched_at") for item in evidence_rows]
    if max(availabilities) > cutoff:
        return unavailable_snapshot(day, generated_at, mode, "source_available_after_cutoff", sources=evidence)
    same_contract = prior is not None and prior.get("contract") == anchor.get("contract")
    high, low, opening = (pit.optional_number(anchor.get(key)) for key in ("high", "low", "open"))
    days_to_expiry = anchor.get("days_to_expiry")
    prior_close = pit.optional_number(prior.get("close")) if same_contract else None
    previous_return = pit.same_contract_return(anchor, prior)
    volatility = pit.trailing_return_volatility(sessions, index - 1, by_date)
    roll = bool(anchor.get("is_expiry_week") or (isinstance(days_to_expiry, int) and 0 <= days_to_expiry <= 7)
                or (prior is not None and not same_contract) or anchor.get("roll_date") == anchor_day.isoformat())
    features = {
        "tx_previous_day_return": previous_return,
        "tx_open_to_close_return": pit.ratio_change(anchor.get("close"), anchor.get("open")),
        "tx_intraday_range_pct": (high - low) / opening if high is not None and low is not None and opening not in {None, 0} else None,
        "tx_true_range_pct": max(high - low, abs(high - prior_close), abs(low - prior_close)) / prior_close
            if high is not None and low is not None and prior_close not in {None, 0} else None,
        "tx_return_volatility_5d": volatility,
        "tx_volume": pit.optional_number(anchor.get("volume")),
        "tx_volume_change_1d": pit.ratio_change(anchor.get("volume"), prior.get("volume") if same_contract else None),
        "tx_open_interest": pit.optional_number(anchor.get("open_interest")),
        "tx_open_interest_change_1d": pit.difference(anchor.get("open_interest"), prior.get("open_interest") if same_contract else None),
        "tx_open_interest_change_pct": pit.ratio_change(anchor.get("open_interest"), prior.get("open_interest") if same_contract else None),
        "tx_days_to_expiry": float(days_to_expiry) if isinstance(days_to_expiry, (int, float)) else None,
        "tx_expiry_week": bool(anchor.get("is_expiry_week") or (isinstance(days_to_expiry, int) and 0 <= days_to_expiry <= 7)),
        "tx_roll_regime": roll,
    }
    return {
        "snapshot_version": VERSION, "status": "ready", "mode": mode,
        "prediction_date": day.isoformat(), "prediction_at": cutoff.isoformat(), "generated_at": generated_at.isoformat(),
        "contract": anchor["contract"], "source_date": anchor_day.isoformat(),
        "max_feature_available_at": max(availabilities).astimezone(timezone.utc).isoformat(),
        # Conservative per-feature evidence: each Core value is no earlier
        # than the latest source used by this snapshot.
        "feature_available_at": {name: max(availabilities).astimezone(timezone.utc).isoformat() for name in FEATURE_COLUMNS},
        "source_fetched_at": max(fetched).astimezone(timezone.utc).isoformat(),
        "sources": evidence, "feature_columns": list(FEATURE_COLUMNS), **features,
    }


def write_snapshot(snapshot: dict[str, Any], root: Path) -> Path:
    """Publish a complete JSON file without allowing an existing date overwrite."""
    day = snapshot["prediction_date"]
    path = root / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{day}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            json.dump(snapshot, target, ensure_ascii=False, sort_keys=True, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        # hard-link publication is create-only, unlike os.replace.
        os.link(temporary_name, path)
    except FileExistsError:
        raise FileExistsError(f"immutable snapshot already exists: {path}")
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return path


def run(options: argparse.Namespace, *, now: datetime | None = None) -> dict[str, Any]:
    started_at = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    snapshot = build_snapshot(options.prediction_date, read_tx_rows(options.tx_root), generated_at=started_at, mode=options.mode)
    # This is deliberately set only once all source reads and feature work have
    # completed; callers must not backdate it to the scheduler start time.
    completed_at = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    snapshot["generated_at"] = completed_at.isoformat()
    if snapshot.get("status") == "ready":
        fetched = max(_aware(source["fetched_at"], "source fetched_at") for source in snapshot["sources"])
        if fetched > completed_at:
            snapshot = unavailable_snapshot(options.prediction_date, completed_at, options.mode,
                                            "source_not_yet_locally_fetched", sources=snapshot["sources"])
    path = write_snapshot(snapshot, options.output_root)
    return {"snapshot": str(path), "status": snapshot["status"], "reason": snapshot.get("unavailable_reason"),
            "generated_at": completed_at.isoformat()}


if __name__ == "__main__":
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
