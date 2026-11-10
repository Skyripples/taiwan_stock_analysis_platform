"""Read-only US_PRE / US_POST realised-label builder for physical TX 5-minute bars.

Historical labels use a deterministic, calendar-backed TX front-contract rule.
They intentionally do *not* select a contract from whatever contracts happen
to appear in a completed price file.  Separately, live readiness remains
strict: reconstructed end-of-day files cannot prove that a bar, contract, or
feature was acquired by an intraday cutoff.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from taifex_trading_calendar import is_taifex_trading_day, taifex_session_arrangements

from research.futures_intraday_targets import (
    INTERVAL,
    NEW_YORK,
    UTC,
    US_CALENDAR_PATH,
    aware,
    load_us_calendar,
    read_tx_candles,
    us_regular_session,
)
from config import PROJECT_ROOT
from research.futures_v5_task_definitions import US_POST as US_POST_DEFINITION, US_PRE as US_PRE_DEFINITION


US_PRE = "us_pre_0900_to_1000_tx_return"
US_POST = "us_post_1000_to_1100_tx_return"
TARGET_NAMES = (US_PRE, US_POST)
EXPIRY_RULE_PATH = PROJECT_ROOT / "config" / "taifex_tx_expiry_rules.json"
TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class TargetWindow:
    name: str
    cutoff_at: datetime
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class HistoricalContractSelection:
    contract: str | None
    expiry_date: date | None
    evidence_status: str
    basis: dict[str, Any]


def _utc(value: datetime) -> datetime:
    return aware(value, "timestamp")


def _after_hours_anchor_date(rows: Iterable[dict[str, Any]]) -> date | None:
    """Return the NY calendar date on which this TX night session began."""
    local_dates = [
        _utc(row["timestamp_utc"]).astimezone(NEW_YORK).date()
        for row in rows
        if row.get("session") == "after_hours"
    ]
    return min(local_dates) if local_dates else None


def us_target_windows(us_date: date, calendar: dict[str, Any]) -> tuple[TargetWindow, ...] | None:
    """Return verified NYSE-day windows, including DST through zoneinfo.

    The NYSE calendar is used as a verified open-day calendar.  These windows
    are pre-market relative to the NYSE 09:30 open, so early close does not
    alter their endpoints.
    """
    if us_regular_session(us_date, calendar) is None:
        return None
    windows = []
    for name, definition in ((US_PRE, US_PRE_DEFINITION), (US_POST, US_POST_DEFINITION)):
        start_wall = time.fromisoformat(definition.start)
        end_wall = time.fromisoformat(definition.end)
        windows.append(TargetWindow(name, datetime.combine(us_date, start_wall, NEW_YORK).astimezone(UTC),
                                    datetime.combine(us_date, start_wall, NEW_YORK).astimezone(UTC),
                                    datetime.combine(us_date, end_wall, NEW_YORK).astimezone(UTC)))
    return tuple(windows)


def _completed_close(rows: Iterable[dict[str, Any]], endpoint: datetime, contract: str) -> dict[str, Any] | None:
    """Return the close of exactly the 5m bar ending at ``endpoint``."""
    expected_start = endpoint - INTERVAL
    for row in rows:
        if (
            row.get("session") == "after_hours"
            and row.get("contract") == contract
            and _utc(row["timestamp_utc"]) == expected_start
        ):
            return row
    return None


def _third_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return date(year, month, 1 + (2 - first.weekday()) % 7 + 14)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def load_tx_expiry_rules(path: Path = EXPIRY_RULE_PATH) -> dict[str, Any]:
    """Load versioned, official-rule provenance; malformed input fails closed."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    default = payload.get("default_rule")
    if payload.get("product") != "TX" or payload.get("timezone") != "Asia/Taipei" or not isinstance(default, dict):
        raise ValueError("invalid TX expiry-rule configuration")
    if not default.get("source_url") or not default.get("evidence_status"):
        raise ValueError("TX expiry default rule lacks provenance")
    if not isinstance(payload.get("exceptions"), list):
        raise ValueError("TX expiry exceptions must be a list")
    return payload


def _next_scheduled_taifex_day(value: date) -> date | None:
    candidate = value
    for _ in range(31):
        try:
            if is_taifex_trading_day(candidate):
                return candidate
        except (OSError, TypeError, ValueError):
            return None
        candidate += timedelta(days=1)
    return None


def _previous_scheduled_taifex_day_for_legacy_reference(value: date) -> date | None:
    """Replicate the pre-fix backward substitution for audit comparison only."""
    candidate = value
    for _ in range(31):
        try:
            if is_taifex_trading_day(candidate):
                return candidate
        except (OSError, TypeError, ValueError):
            return None
        candidate -= timedelta(days=1)
    return None


def _exception_for(contract: str, rules: dict[str, Any]) -> dict[str, Any] | None:
    matches = [item for item in rules["exceptions"] if item.get("contract") == contract]
    if len(matches) > 1:
        raise ValueError(f"duplicate TX expiry exception: {contract}")
    return matches[0] if matches else None


def _selection_from_exception(contract: str, cutoff_at: datetime, exception: dict[str, Any]) -> HistoricalContractSelection:
    try:
        expiry_at = datetime.fromisoformat(exception["expiry_at"]).astimezone(UTC)
        available_at = datetime.fromisoformat(exception["available_at"]).astimezone(UTC)
        expiry_date = date.fromisoformat(exception["last_trading_date"])
    except (KeyError, TypeError, ValueError) as error:
        return HistoricalContractSelection(None, None, "unavailable_contract_rule", {
            "contract": contract, "reason": f"invalid official expiry exception: {error}",
        })
    evidence_status = "verified_official_exception" if available_at <= cutoff_at else "unverified_exception_not_known_by_cutoff"
    return HistoricalContractSelection(contract, expiry_date, evidence_status, {
        "rule": "official TX expiry exception",
        "contract": contract,
        "expiry_at": expiry_at.isoformat(),
        "source_name": exception.get("source_name"),
        "source_url": exception.get("source_url"),
        "available_at": available_at.isoformat(),
        "cutoff_at": cutoff_at.isoformat(),
        "evidence_note": exception.get("evidence_note"),
    })


def historical_front_contract(cutoff_at: datetime, rules: dict[str, Any] | None = None) -> HistoricalContractSelection:
    """Choose the nearest unexpired listed monthly TX contract from formal rules.

    The rule is independent of prices, intraday file contents, volume, OI, and
    target endpoints.  A formal per-contract announcement overrides the
    default.  Otherwise, the official default is third Wednesday, moving to
    the next TAIFEX business day if that day cannot trade.  The default rule's
    historical publication vintage is deliberately reported as unverified.
    """
    cutoff_at = _utc(cutoff_at)
    rules = rules or load_tx_expiry_rules()
    taipei_cutoff = cutoff_at.astimezone(TAIPEI)
    year, month = taipei_cutoff.year, taipei_cutoff.month
    for _ in range(13):
        contract = f"TX{year:04d}{month:02d}"
        exception = _exception_for(contract, rules)
        if exception is not None:
            selected = _selection_from_exception(contract, cutoff_at, exception)
            expiry_at_text = selected.basis.get("expiry_at")
            if selected.contract is None or expiry_at_text is None:
                return selected
            if cutoff_at <= datetime.fromisoformat(expiry_at_text):
                return selected
            year, month = _next_month(year, month)
            continue
        scheduled_expiry = _next_scheduled_taifex_day(_third_wednesday(year, month))
        if scheduled_expiry is None:
            return HistoricalContractSelection(
                None, None, "unavailable_contract_rule",
                {"rule": "TX nearest unexpired monthly contract", "applicable_cutoff": cutoff_at.isoformat(),
                 "reason": "TAIFEX calendar or formal exception coverage unavailable"},
            )
        expiry_at = datetime.combine(scheduled_expiry, time(13, 30), TAIPEI).astimezone(UTC)
        if cutoff_at <= expiry_at:
            return HistoricalContractSelection(
                contract, scheduled_expiry, rules["default_rule"]["evidence_status"],
                {
                    "rule": "TX nearest unexpired monthly contract",
                    "expiry_rule": rules["default_rule"]["last_trading_day"],
                    "expiry_at": expiry_at.isoformat(),
                    "source_name": rules["default_rule"].get("source_name"),
                    "source_url": rules["default_rule"]["source_url"],
                    "cutoff_at": cutoff_at.isoformat(),
                    "evidence_note": rules["default_rule"].get("evidence_note"),
                    "expiry_date": scheduled_expiry.isoformat(),
                },
            )
        year, month = _next_month(year, month)
    return HistoricalContractSelection(None, None, "unavailable_contract_rule", {
        "rule": "TX nearest unexpired monthly contract", "applicable_cutoff": cutoff_at.isoformat(),
        "reason": "could not resolve a listed monthly contract",
    })


def pre_fix_front_contract_reference(trading_date: date) -> HistoricalContractSelection:
    """Deterministic representation of the prior, incorrect rule for audit only.

    It deliberately has no role in current labels.  The original implementation
    used trading date rather than actual expiry time and moved an unavailable
    third Wednesday *backwards*, so its evidence status is historical code
    output—not evidence that the rule was ever valid.
    """
    year, month = trading_date.year, trading_date.month
    for _ in range(13):
        expiry = _previous_scheduled_taifex_day_for_legacy_reference(_third_wednesday(year, month))
        if expiry is None:
            return HistoricalContractSelection(None, None, "prior_rule_unavailable", {"rule": "pre-fix reference"})
        if trading_date <= expiry:
            return HistoricalContractSelection(
                f"TX{year:04d}{month:02d}", expiry, "verified_historical_rule",
                {"rule": "pre-fix backwards calendar substitution", "expiry_date": expiry.isoformat()},
            )
        year, month = _next_month(year, month)
    return HistoricalContractSelection(None, None, "prior_rule_unavailable", {"rule": "pre-fix reference"})


def live_contract_evidence(rows: Iterable[dict[str, Any]], cutoff_at: datetime, contract: str | None) -> str:
    """Check strict live cutoff evidence without influencing historical labels."""
    if contract is None:
        return "unknown_no_historical_contract"
    saw_contract = False
    for row in rows:
        if row.get("session") != "after_hours" or row.get("contract") != contract:
            continue
        saw_contract = True
        try:
            completed_at = _utc(row["timestamp_utc"]) + INTERVAL
            if completed_at <= cutoff_at and _utc(row["available_at"]) <= cutoff_at and _utc(row["fetched_at"]) <= cutoff_at:
                return "passed_cutoff_evidence"
        except (KeyError, TypeError, ValueError):
            return "unknown_invalid_live_provenance"
    return "failed_no_cutoff_acquisition_evidence" if saw_contract else "unknown_contract_not_in_price_file"


def _direction(value: float | None) -> str | None:
    if value is None:
        return None
    if value > 0:
        return "UP"
    if value < 0:
        return "DOWN"
    return "FLAT"


def _record(window: TargetWindow, trading_date: date, selection: HistoricalContractSelection,
            start: dict[str, Any] | None, end: dict[str, Any] | None, *, reason: str | None = None,
            live_evidence_status: str = "not_assessed") -> dict[str, Any]:
    if reason is None and selection.contract is None:
        reason = selection.evidence_status
    if reason is None and (start is None or end is None):
        reason = "missing_required_endpoint"
    result: float | None = None
    target_available_at: datetime | None = None
    if reason is None:
        try:
            start_price = float(start["close"])
            end_price = float(end["close"])
            if start_price == 0:
                reason = "invalid_start_price"
            else:
                result = end_price / start_price - 1.0
                target_available_at = max(_utc(start["available_at"]), _utc(end["available_at"]))
        except (KeyError, TypeError, ValueError):
            reason = "invalid_required_price"
    return {
        "target_name": window.name,
        "prediction_at": window.cutoff_at,
        "trading_date": trading_date,
        "contract": selection.contract,
        "contract_expiry_date": selection.expiry_date,
        "contract_selection_evidence_status": selection.evidence_status,
        "contract_selection_basis": selection.basis,
        "start_at": window.start_at,
        "end_at": window.end_at,
        "start_price": None if start is None else start.get("close"),
        "end_price": None if end is None else end.get("close"),
        "target_return": result,
        "direction_class": _direction(result),
        "target_available_at": target_available_at,
        "start_source": None if start is None else start.get("source"),
        "end_source": None if end is None else end.get("source"),
        "feature_pit_status": "not_assessed_labels_only",
        "live_contract_evidence_status": live_evidence_status,
        "live_readiness": (
            "not_ready_historical_reconstruction_only"
            if live_evidence_status != "passed_cutoff_evidence" else "unknown_feature_pipeline_not_assessed"
        ),
        "missing_reason": reason,
    }


def build_us_pre_post_targets(candles: Iterable[dict[str, Any]], us_calendar: dict[str, Any]) -> list[dict[str, Any]]:
    """Build null-preserving labels without writing a dataset or touching inputs."""
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for original in candles:
        row = dict(original)
        if row.get("product") != "TX" or row.get("interval_seconds") != 300:
            continue
        if not isinstance(row.get("trading_date"), date):
            raise ValueError("trading_date must be a date")
        row["timestamp_utc"] = _utc(row["timestamp_utc"])
        row["available_at"] = _utc(row["available_at"])
        row["fetched_at"] = _utc(row["fetched_at"])
        grouped[row["trading_date"]].append(row)

    output: list[dict[str, Any]] = []
    for trading_date, day_rows in sorted(grouped.items()):
        after_hours = [row for row in day_rows if row.get("session") == "after_hours"]
        arrangement = taifex_session_arrangements(trading_date).get("after_hours", {})
        us_date = _after_hours_anchor_date(after_hours)
        windows = us_target_windows(us_date, us_calendar) if us_date else None
        for name in TARGET_NAMES:
            if not after_hours:
                # A source partition alone is never evidence of a session.
                fallback = datetime.combine(trading_date, time.min, UTC)
                selection = historical_front_contract(fallback)
                output.append(_record(TargetWindow(name, fallback, fallback, fallback), trading_date, selection, None, None,
                                      reason="missing_after_hours_session"))
                continue
            if arrangement.get("state") != "expected":
                fallback = datetime.combine(trading_date, time.min, UTC)
                selection = historical_front_contract(fallback)
                output.append(_record(TargetWindow(name, fallback, fallback, fallback), trading_date, selection, None, None,
                                      reason="taifex_after_hours_not_scheduled"))
                continue
            if windows is None:
                fallback = datetime.combine(us_date or trading_date, time.min, NEW_YORK).astimezone(UTC)
                selection = historical_front_contract(fallback)
                output.append(_record(TargetWindow(name, fallback, fallback, fallback), trading_date, selection, None, None,
                                      reason="unverified_or_closed_us_session"))
                continue
            window = next(item for item in windows if item.name == name)
            selection = historical_front_contract(window.cutoff_at)
            start = _completed_close(after_hours, window.start_at, selection.contract) if selection.contract else None
            end = _completed_close(after_hours, window.end_at, selection.contract) if selection.contract else None
            record = _record(
                window, trading_date, selection, start, end,
                live_evidence_status=live_contract_evidence(after_hours, window.cutoff_at, selection.contract),
            )
            prior = pre_fix_front_contract_reference(trading_date)
            prior_start = _completed_close(after_hours, window.start_at, prior.contract) if prior.contract else None
            prior_end = _completed_close(after_hours, window.end_at, prior.contract) if prior.contract else None
            prior_record = _record(window, trading_date, prior, prior_start, prior_end)
            record["pre_fix_comparison"] = {
                "reference_note": "Computed comparison to prior code rule; not a persisted historical audit artifact.",
                "contract": prior.contract,
                "expiry_date": prior.expiry_date,
                "target_return": prior_record["target_return"],
                "direction_class": prior_record["direction_class"],
                "selection_evidence_status": prior.evidence_status,
                "contract_changed": prior.contract != record["contract"],
                "expiry_changed": prior.expiry_date != record["contract_expiry_date"],
                "label_changed": prior_record["target_return"] != record["target_return"],
                "selection_evidence_changed": prior.evidence_status != record["contract_selection_evidence_status"],
            }
            output.append(record)
    # Closed / uncovered US sessions do not have endpoint prices to replay, but
    # still receive a contract/evidence comparison rather than silently
    # disappearing from the before/after audit.
    for record in output:
        if "pre_fix_comparison" in record:
            continue
        prior = pre_fix_front_contract_reference(record["trading_date"])
        record["pre_fix_comparison"] = {
            "reference_note": "Computed from the former rule; no persisted historical audit artifact was overwritten.",
            "contract": prior.contract,
            "expiry_date": prior.expiry_date,
            "target_return": None,
            "direction_class": None,
            "selection_evidence_status": prior.evidence_status,
            "contract_changed": prior.contract != record["contract"],
            "expiry_changed": prior.expiry_date != record["contract_expiry_date"],
            "label_changed": False,
            "selection_evidence_changed": prior.evidence_status != record["contract_selection_evidence_status"],
        }
    return output


def load_and_build(root=None, calendar_path=US_CALENDAR_PATH) -> list[dict[str, Any]]:
    """Convenience entry point for a read-only local audit."""
    return build_us_pre_post_targets(read_tx_candles(root) if root is not None else read_tx_candles(),
                                     load_us_calendar(calendar_path))
