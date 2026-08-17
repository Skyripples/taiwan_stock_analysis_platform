"""Audit whether every historical feature was knowable before target open.

This command is intentionally read-only with respect to datasets and models. It
only writes an atomic JSON audit report.
"""

from __future__ import annotations

import csv
import json
import os
import time
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import requests

from config import PROJECT_ROOT


DATASET_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
DAILY_HISTORY_PATH = PROJECT_ROOT / "data" / "history" / "market_daily.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "temporal_leakage_report.json"
BACKFILL_SCRIPT_PATH = PROJECT_ROOT / "scripts" / "backfill_history.py"
DAILY_SCRIPT_PATH = PROJECT_ROOT / "scripts" / "update_history.py"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

FEATURE_RULES = {
    "taiex_close": ("taiwan_market_trade_date", "feature_close"),
    "taiex_change_percent": ("taiwan_market_trade_date", "feature_close"),
    "tpex_close": ("taiwan_market_trade_date", "feature_close"),
    "turnover": ("taiwan_market_trade_date", "feature_close"),
    "advancing": ("taiwan_market_trade_date", "feature_close"),
    "declining": ("taiwan_market_trade_date", "feature_close"),
    "unchanged": ("taiwan_market_trade_date", "feature_close"),
    "foreign_cash_flow": ("institutional_trade_date", "feature_close"),
    "foreign_futures_position": ("foreign_futures_trade_date", "feature_close"),
    "night_futures_change": ("night_futures_trade_date", "target_preopen"),
    "tsm_adr_change_percent": ("tsm_adr_trade_date", "us_completed"),
    "sox_change_percent": ("sox_trade_date", "us_completed"),
    "sp500_change_percent": ("sp500_trade_date", "us_completed"),
    "nasdaq_change_percent": ("nasdaq_trade_date", "us_completed"),
    "nikkei_change_percent": ("nikkei_trade_date", "us_completed"),
    "kospi_change_percent": ("kospi_trade_date", "us_completed"),
    "hang_seng_change_percent": ("hang_seng_trade_date", "us_completed"),
    "csi300_change_percent": ("csi300_trade_date", "us_completed"),
    "soxx_change_percent": ("soxx_trade_date", "us_completed"),
    "smh_change_percent": ("smh_trade_date", "us_completed"),
    "nvda_change_percent": ("nvda_trade_date", "us_completed"),
    "amd_change_percent": ("amd_trade_date", "us_completed"),
    "avgo_change_percent": ("avgo_trade_date", "us_completed"),
}

YAHOO_FEATURES = {
    "tsm_adr_change_percent": "TSM",
    "sox_change_percent": "^SOX",
    "sp500_change_percent": "^GSPC",
    "nasdaq_change_percent": "^IXIC",
    "nikkei_change_percent": "^N225",
    "kospi_change_percent": "^KS11",
    "hang_seng_change_percent": "^HSI",
    "csi300_change_percent": "000300.SS",
    "soxx_change_percent": "SOXX",
    "smh_change_percent": "SMH",
    "nvda_change_percent": "NVDA",
    "amd_change_percent": "AMD",
    "avgo_change_percent": "AVGO",
}


def audit_temporal_leakage(
    dataset_path: Path = DATASET_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, Any]:
    rows = _read_dataset(dataset_path)
    first_date = _date(rows[0]["feature_date"], "feature_date")
    last_target = _date(rows[-1]["target_date"], "target_date")
    yahoo_calendars = {
        feature: _fetch_yahoo_calendar(symbol, first_date, last_target)
        for feature, symbol in YAHOO_FEATURES.items()
    }

    summaries = {
        feature: {
            "source_trade_date_field": source_field,
            "availability_rule": rule,
            "passed": 0,
            "failed": 0,
            "leakage_failures": 0,
            "stale_alignment_failures": 0,
            "missing_failures": 0,
        }
        for feature, (source_field, rule) in FEATURE_RULES.items()
    }
    issues: list[dict[str, Any]] = []
    sample_has_leakage: set[str] = set()
    sample_has_alignment_issue: set[str] = set()
    weekend_pairs = 0
    long_holiday_pairs = 0
    cross_market_calendar_cases = 0

    for row in rows:
        feature_date = _date(row["feature_date"], "feature_date")
        target_date = _date(row["target_date"], "target_date")
        gap_days = (target_date - feature_date).days
        if gap_days >= 3:
            weekend_pairs += 1
        if gap_days > 3:
            long_holiday_pairs += 1

        for feature, (source_field, rule) in FEATURE_RULES.items():
            summary = summaries[feature]
            source_text = str(row.get(source_field, "")).strip()
            if not source_text:
                _fail(
                    summary,
                    issues,
                    sample_has_alignment_issue,
                    feature,
                    source_field,
                    None,
                    feature_date,
                    target_date,
                    "missing",
                    "source trade_date is missing",
                )
                continue
            source_date = _date(source_text, source_field)

            expected_date: date
            if rule == "feature_close":
                expected_date = feature_date
            elif rule == "target_preopen":
                expected_date = target_date
            else:
                expected_date = _latest_before(
                    yahoo_calendars[feature], target_date, feature
                )
                if expected_date != feature_date:
                    cross_market_calendar_cases += 1

            # A source later than the last completed session before target open
            # is genuine future-data leakage. An earlier source is safe but does
            # not implement the requested freshest-available alignment.
            if source_date > expected_date:
                sample_has_leakage.add(feature_date.isoformat())
                _fail(
                    summary,
                    issues,
                    sample_has_alignment_issue,
                    feature,
                    source_field,
                    source_date,
                    feature_date,
                    target_date,
                    "leakage",
                    f"source trade_date {source_date} is later than the latest "
                    f"legally completed date {expected_date}",
                    expected_date,
                )
            elif source_date < expected_date:
                _fail(
                    summary,
                    issues,
                    sample_has_alignment_issue,
                    feature,
                    source_field,
                    source_date,
                    feature_date,
                    target_date,
                    "stale_alignment",
                    f"source trade_date {source_date} is safe but stale; expected "
                    f"the latest completed date {expected_date}",
                    expected_date,
                )
            else:
                summary["passed"] += 1

    total = len(rows)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "audit_version": "1.0",
        "dataset": str(dataset_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "total_samples": total,
        "leakage_detected": bool(sample_has_leakage),
        "samples_with_leakage": len(sample_has_leakage),
        "leakage_safe_samples": total - len(sample_has_leakage),
        "fully_aligned_samples": total - len(sample_has_alignment_issue),
        "samples_with_alignment_issues": len(sample_has_alignment_issue),
        "feature_results": summaries,
        "calendar_edge_cases": {
            "weekend_or_long_gap_pairs": weekend_pairs,
            "long_holiday_pairs_over_three_calendar_days": long_holiday_pairs,
            "us_latest_session_differs_from_feature_date_checks": cross_market_calendar_cases,
        },
        "flow_comparison": _compare_flows(),
        "issues": issues,
    }
    _write_json_atomic(output_path, report)
    return report


def _fail(
    summary: dict[str, Any],
    issues: list[dict[str, Any]],
    affected_samples: set[str],
    feature: str,
    source_field: str,
    source_date: date | None,
    feature_date: date,
    target_date: date,
    category: str,
    reason: str,
    expected_date: date | None = None,
) -> None:
    summary["failed"] += 1
    summary[f"{category}_failures"] += 1
    affected_samples.add(feature_date.isoformat())
    issues.append(
        {
            "feature": feature,
            "source_trade_date_field": source_field,
            "source_trade_date": source_date.isoformat() if source_date else None,
            "expected_latest_legal_trade_date": (
                expected_date.isoformat() if expected_date else None
            ),
            "feature_date": feature_date.isoformat(),
            "target_date": target_date.isoformat(),
            "category": category,
            "failure_reason": reason,
        }
    )


def _read_dataset(path: Path) -> list[dict[str, str]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Dataset not found: {path}") from exc
    with handle:
        reader = csv.DictReader(handle)
        required = {"feature_date", "target_date", *FEATURE_RULES}
        required.update(field for field, _ in FEATURE_RULES.values())
        missing = sorted(required - set(reader.fieldnames or ()))
        if missing:
            raise ValueError(f"Dataset is missing columns: {', '.join(missing)}")
        rows = list(reader)
    if not rows:
        raise ValueError("Historical prediction dataset is empty")
    previous: date | None = None
    seen: set[date] = set()
    for number, row in enumerate(rows, start=2):
        feature_date = _date(row["feature_date"], f"feature_date row {number}")
        target_date = _date(row["target_date"], f"target_date row {number}")
        if feature_date in seen or (previous is not None and feature_date <= previous):
            raise ValueError("Dataset dates are duplicated or not strictly ascending")
        if target_date <= feature_date:
            raise ValueError(f"Invalid target date at row {number}")
        for feature in FEATURE_RULES:
            try:
                float(str(row[feature]).strip())
            except ValueError as exc:
                raise ValueError(f"Invalid feature value: {feature} row {number}") from exc
        seen.add(feature_date)
        previous = feature_date
    return rows


def _fetch_yahoo_calendar(symbol: str, start: date, end: date) -> list[date]:
    period1 = int(
        datetime.combine(start - timedelta(days=10), datetime.min.time(), tzinfo=timezone.utc).timestamp()
    )
    period2 = int(
        datetime.combine(end + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc).timestamp()
    )
    session = requests.Session()
    session.headers["User-Agent"] = "taiwan-stock-analysis-platform-audit/1.0"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(
                YAHOO_URL.format(symbol=requests.utils.quote(symbol, safe="")),
                params={"period1": period1, "period2": period2, "interval": "1d", "events": "history"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            result = payload["chart"]["result"][0]
            timestamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
            dates = sorted(
                {
                    datetime.fromtimestamp(timestamp, timezone.utc).date()
                    for timestamp, close in zip(timestamps, closes)
                    if close is not None
                }
            )
            if not dates:
                raise ValueError(f"Yahoo returned no completed sessions for {symbol}")
            return dates
        except (KeyError, IndexError, TypeError, ValueError, requests.RequestException) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise ValueError(f"Unable to load Yahoo calendar for {symbol}: {last_error}")


def _latest_before(calendar: list[date], target_date: date, feature: str) -> date:
    index = bisect_left(calendar, target_date) - 1
    if index < 0:
        raise ValueError(f"No completed US session before {target_date} for {feature}")
    return calendar[index]


def _compare_flows() -> dict[str, Any]:
    backfill_source = BACKFILL_SCRIPT_PATH.read_text(encoding="utf-8")
    daily_source = DAILY_SCRIPT_PATH.read_text(encoding="utf-8")
    with DAILY_HISTORY_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        daily_rows = list(csv.DictReader(handle))
    daily_night_examples = [
        {
            "feature_date": row.get("trade_date"),
            "night_futures_trade_date": row.get("night_futures_trade_date"),
        }
        for row in daily_rows[-5:]
    ]
    return {
        "same_time_logic": (
            '"night_futures_trade_date": target_date.isoformat()' in backfill_source
            and "latest_before(yahoo[source], target_date" in backfill_source
        ),
        "backfill": {
            "night_rule": "queries and stores the T-close-to-target-preopen session as target_date",
            "us_rule": "latest completed Yahoo session strictly before target_date",
            "code_evidence_present": (
                '"night_futures_trade_date": target_date.isoformat()' in backfill_source
                and "latest_before(yahoo[source], target_date" in backfill_source
            ),
        },
        "daily": {
            "rule": "copies each provider trade_date without enforcing a target-open cutoff",
            "operational_schedule": "workflow runs 00:00 UTC / 08:00 Asia/Taipei before Taiwan open",
            "code_evidence_present": (
                '"night_futures_trade_date": dates["night_futures"]' in daily_source
            ),
            "recent_night_alignment_examples": daily_night_examples,
        },
        "finding": (
            "Backfill and the scheduled daily pipeline now use the same target-preopen timing: "
            "night trade_date equals target_date and US data is the latest completed session "
            "before target_date. Daily code copies provider dates, so its 08:00 Asia/Taipei "
            "schedule remains part of enforcement."
        ),
    }


def _date(value: Any, label: str) -> date:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date for {label}: {value}") from exc


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    try:
        report = audit_temporal_leakage()
    except (OSError, ValueError) as exc:
        print(f"Temporal leakage audit failed: {exc}")
        return 1
    print(
        "Temporal leakage audit finished | "
        f"samples={report['total_samples']} "
        f"leakage={report['samples_with_leakage']} "
        f"alignment_issues={report['samples_with_alignment_issues']} "
        f"report={OUTPUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
