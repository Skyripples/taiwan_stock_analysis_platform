"""Shared temporal contract for production next-session prediction inputs."""

from __future__ import annotations

from datetime import date

from trading_calendar import get_next_trading_day


FEATURE_DATE_SOURCES = {"institutional", "foreign_futures"}
PREOPEN_DATE_SOURCES = {
    "tsm_adr",
    "sox",
    "sp500",
    "nasdaq",
    "vix",
    "kospi",
}


def prediction_target_date(feature_date: str) -> str | None:
    """Return the covered official next TWSE session, never a later available row."""

    return get_next_trading_day(feature_date)


def source_date_is_valid(
    source_name: str,
    source_date: str,
    feature_date: str,
    target_date: str,
    *,
    max_age_days: int | None = None,
) -> bool:
    """Apply the production pre-open date contract to one source."""

    source = date.fromisoformat(source_date)
    feature = date.fromisoformat(feature_date)
    target = date.fromisoformat(target_date)

    if source_name == "taiwan_market":
        return source == feature
    if source_name in FEATURE_DATE_SOURCES:
        valid = source <= feature
        age = (feature - source).days
    elif source_name == "night_futures":
        return feature <= source <= target
    elif source_name in PREOPEN_DATE_SOURCES:
        valid = source < target
        age = (target - source).days
    else:
        raise ValueError(f"Unknown prediction source: {source_name}")

    return valid and (max_age_days is None or age <= max_age_days)


def source_date_requirement(source_name: str, feature_date: str, target_date: str) -> str:
    if source_name == "taiwan_market":
        return f"equal feature date {feature_date}"
    if source_name in FEATURE_DATE_SOURCES:
        return f"not exceed feature date {feature_date}"
    if source_name == "night_futures":
        return f"be between feature date {feature_date} and target date {target_date}"
    if source_name in PREOPEN_DATE_SOURCES:
        return f"precede target date {target_date}"
    raise ValueError(f"Unknown prediction source: {source_name}")
