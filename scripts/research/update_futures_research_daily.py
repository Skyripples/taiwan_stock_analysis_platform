"""Run the bounded, incremental daily Futures research pipeline locally.

This command deliberately orchestrates existing update/build components.  It
does not introduce a second TAIFEX, Yahoo, FRED, institutional, or PCR parser,
and it never invokes a historical backfill range.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from config import PROJECT_ROOT
import update_futures_daily as tx_updater
from providers.taifex_provider import TaifexProvider
from research import backfill_futures_institutional_positions as institutional
from research import build_futures_daily_pit_alignment as pit_builder
from research import build_futures_daily_targets as target_builder
from research import build_futures_research_dataset as dataset_builder
from research import update_taifex_pcr_latest as pcr_updater


TAIPEI = ZoneInfo("Asia/Taipei")
GLOBAL_SYMBOLS = ("SOX", "TSM_ADR", "NASDAQ", "SP500", "USDTWD", "DGS10")
DEFAULT_LOOKBACK_DAYS = 10


@dataclass(frozen=True)
class StageResult:
    status: str
    details: dict[str, Any]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--end", type=lambda value: date.today() if value == "today" else date.fromisoformat(value), default=date.today())
    parser.add_argument("--delay", type=float, default=2.5, help="Existing TX updater request delay")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; no network or writes")
    options = parser.parse_args()
    if not 1 <= options.lookback_days <= 14:
        parser.error("--lookback-days must be between 1 and 14 for the incremental pipeline")
    if options.delay < 0:
        parser.error("--delay cannot be negative")
    return options


def source_end(options: argparse.Namespace) -> date:
    """Latest Taiwan session expected to be complete for the requested run."""
    return tx_updater.latest_completed_trading_day(datetime.combine(options.end, wall_time(16, 0), TAIPEI))


def window_start(options: argparse.Namespace) -> date:
    return source_end(options) - timedelta(days=options.lookback_days)


def research_end(options: argparse.Namespace) -> date:
    """Never build beyond the latest actual TX staging session."""
    dates = tx_updater.tx_staging_dates()
    if not dates:
        raise ValueError("TX staging contains no official daily rows")
    return min(source_end(options), max(dates))


def stage_tx(options: argparse.Namespace) -> StageResult:
    # ``now`` makes --end deterministic while retaining the updater's official
    # calendar and completed-session cut-off rules.
    completed_at = datetime.combine(options.end, wall_time(16, 0), TAIPEI)
    result = tx_updater.run_staging_only(
        argparse.Namespace(products=["TX"], lookback_days=options.lookback_days, delay=options.delay, dry_run=False),
        now=completed_at,
    )
    if result["failures"]:
        raise RuntimeError(f"TX updater failures: {result['failures']}")
    status = "SKIP" if result["skipped_not_yet_published"] else "OK" if result["staging_rows"] else "SKIP"
    return StageResult(status, result)


def stage_global(options: argparse.Namespace, *, execute: Callable[..., Any] = subprocess.run) -> StageResult:
    command = [
        sys.executable, str(PROJECT_ROOT / "scripts" / "research" / "backfill_global_market.py"),
        "--start", window_start(options).isoformat(), "--end", source_end(options).isoformat(),
        "--resume", "--storage", "local",
    ]
    for symbol in GLOBAL_SYMBOLS:
        command.extend(("--symbol", symbol))
    completed = execute(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "global market updater failed").strip())
    return StageResult("OK", {"command": command, "output": completed.stdout.strip()})


def stage_institutional(options: argparse.Namespace, *, provider: TaifexProvider | None = None) -> StageResult:
    provider = provider or TaifexProvider()
    requested = source_end(options)
    raw = provider.fetch_for_date(requested)
    if raw is None:
        return StageResult("SKIP", {"reason": "no_observation", "requested_date": requested.isoformat()})
    rows = institutional.canonical_rows(provider, raw)
    if not rows:
        return StageResult("SKIP", {"reason": "not_yet_available", "trade_date": raw["trade_date"]})
    return StageResult("OK", {
        "trade_date": raw["trade_date"], "written_rows": institutional.write_rows(rows),
    })


def stage_pcr(options: argparse.Namespace) -> StageResult:
    result = pcr_updater.run(argparse.Namespace(date=source_end(options), output=pcr_updater.OUTPUT_ROOT))
    return StageResult("OK" if result["written"] else "SKIP", result)


def stage_pit(options: argparse.Namespace) -> StageResult:
    end = research_end(options)
    result = pit_builder.run(argparse.Namespace(start=end - timedelta(days=options.lookback_days), end=end, output=pit_builder.OUTPUT_ROOT))
    return StageResult("OK" if result["rows"] else "SKIP", result)


def stage_targets(options: argparse.Namespace) -> StageResult:
    end = research_end(options)
    result = target_builder.run(argparse.Namespace(
        start=end - timedelta(days=options.lookback_days), end=end, features=target_builder.FEATURE_ROOT,
        output=target_builder.OUTPUT_ROOT, neutral_threshold=target_builder.DEFAULT_THRESHOLDS.up_min,
    ))
    return StageResult("OK" if result["rows"] else "SKIP", result)


def stage_dataset(options: argparse.Namespace) -> StageResult:
    end = research_end(options)
    result = dataset_builder.run(argparse.Namespace(
        start=end - timedelta(days=options.lookback_days), end=end, features=dataset_builder.FEATURE_ROOT,
        targets=dataset_builder.TARGET_ROOT, output=dataset_builder.OUTPUT_ROOT,
    ))
    return StageResult("OK" if result["rows"] else "SKIP", result)


def audit_rows(feature_rows: list[dict[str, Any]], research_rows: list[dict[str, Any]]) -> dict[str, Any]:
    feature_dates = [row["prediction_date"] for row in feature_rows]
    research_dates = [row["prediction_date"] for row in research_rows]
    duplicates = len(research_dates) - len(set(research_dates))
    violations = sum(
        row.get("max_feature_available_at") is not None
        and row["max_feature_available_at"] > row["prediction_at"]
        for row in feature_rows
    )
    if duplicates:
        raise ValueError(f"Duplicate research prediction_date count: {duplicates}")
    if violations:
        raise ValueError(f"PIT available_at violations: {violations}")
    if not research_rows:
        raise ValueError("Research dataset is not readable or has no rows")
    latest = max(research_rows, key=lambda row: row["prediction_date"])
    return {
        "research_rows": len(research_rows),
        "research_duplicates": duplicates,
        "pit_available_at_violations": violations,
        "latest_prediction_date": max(research_dates).isoformat(),
        "latest_feature_prediction_date": max(feature_dates).isoformat() if feature_dates else None,
        "latest_target_date": latest.get("target_trading_date").isoformat() if latest.get("target_trading_date") else None,
        "latest_target_is_null": latest.get("next_trading_day_regular_return") is None,
    }


def stage_audit(_options: argparse.Namespace) -> StageResult:
    feature_rows = [
        row for path in sorted(pit_builder.OUTPUT_ROOT.rglob("*.parquet"))
        for row in pit_builder.parquet_rows(path)
    ]
    research_rows = dataset_builder.read_rows(dataset_builder.OUTPUT_ROOT, date.min, date.max)
    result = audit_rows(feature_rows, research_rows)
    tx_dates = tx_updater.tx_staging_dates()
    result["latest_tx_staging_date"] = max(tx_dates).isoformat() if tx_dates else None
    return StageResult("OK", result)


def default_stages() -> list[tuple[str, Callable[[argparse.Namespace], StageResult]]]:
    return [
        ("tx_daily_staging", stage_tx),
        ("global_market_factors", stage_global),
        ("institutional_oi", stage_institutional),
        ("pcr", stage_pcr),
        ("pit_features", stage_pit),
        ("targets", stage_targets),
        ("research_dataset", stage_dataset),
        ("audit", stage_audit),
    ]


def run(options: argparse.Namespace, *, stages: list[tuple[str, Callable[[argparse.Namespace], StageResult]]] | None = None) -> dict[str, Any]:
    """Execute stages in dependency order; SKIP is successful, FAIL stops."""
    started = time.monotonic()
    report: dict[str, Any] = {
        "start": window_start(options).isoformat(), "end": source_end(options).isoformat(),
        "requested_end": options.end.isoformat(),
        "lookback_days": options.lookback_days, "dry_run": options.dry_run, "stages": [],
    }
    for name, handler in stages or default_stages():
        print(f"START {name}", flush=True)
        if options.dry_run:
            outcome = StageResult("SKIP", {"reason": "dry_run"})
        else:
            try:
                outcome = handler(options)
            except Exception as exc:
                record = {"name": name, "status": "FAIL", "error": str(exc)}
                report["stages"].append(record)
                report["elapsed_seconds"] = round(time.monotonic() - started, 3)
                print(f"FAIL {name}: {exc}", flush=True)
                return report
        record = {"name": name, "status": outcome.status, **outcome.details}
        report["stages"].append(record)
        print(f"{outcome.status} {name}", flush=True)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def main() -> int:
    result = run(arguments())
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if not any(stage["status"] == "FAIL" for stage in result["stages"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
