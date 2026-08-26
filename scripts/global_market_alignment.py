"""Point-in-time alignment rules for global market research data."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc


def target_cutoff(target_date: date) -> datetime:
    """Return the exclusive TAIEX-open cutoff for one target session."""

    return datetime.combine(target_date, time(9), TAIPEI)


def is_available_before_target(available_at: datetime, target_date: date) -> bool:
    """True only when the datum was public strictly before TAIEX open."""

    if available_at.tzinfo is None:
        raise ValueError("available_at must be timezone-aware")
    return available_at < target_cutoff(target_date)


def us_session_available_at(trade_date: date, hour: int = 16, minute: int = 30) -> datetime:
    """Conservative availability after a completed US cash session."""

    return datetime.combine(trade_date, time(hour, minute), NEW_YORK)


def commodity_available_at(trade_date: date, symbol: str) -> datetime:
    """Conservative post-settlement timestamps for audited CME proxies."""

    settlement = {"HG=F": time(14, 0), "CL=F": time(15, 0), "GC=F": time(14, 0)}
    if symbol not in settlement:
        raise ValueError(f"Unsupported commodity availability rule: {symbol}")
    return datetime.combine(trade_date, settlement[symbol], NEW_YORK)


def fx_available_at(trade_date: date) -> datetime:
    """Daily OTC proxy closes at the conventional 17:00 New York boundary."""

    return datetime.combine(trade_date, time(17, 5), NEW_YORK)


def treasury_available_at(observation_date: date) -> datetime:
    """Conservative same-day timestamp after the US Treasury daily release."""

    return datetime.combine(observation_date, time(16), NEW_YORK)


def icsa_available_at(observation_date: date) -> datetime:
    """Initial claims are released the Thursday after the week-ending Saturday.

    The feature stores this actual scheduled release timestamp rather than
    pretending the Saturday observation date was publicly known on Saturday.
    """

    days_until_thursday = (3 - observation_date.weekday()) % 7
    if days_until_thursday == 0:
        days_until_thursday = 7
    release_date = observation_date + timedelta(days=days_until_thursday)
    return datetime.combine(release_date, time(8, 30), NEW_YORK)


def crypto_available_at(timestamp_utc: datetime, interval_seconds: int = 3600) -> datetime:
    """A candle becomes usable only after its interval has fully closed."""

    if timestamp_utc.tzinfo is None:
        raise ValueError("Crypto timestamp must be timezone-aware")
    return timestamp_utc.astimezone(UTC) + timedelta(seconds=interval_seconds)


def assert_feature_alignment(available_at: datetime, target_date: date) -> None:
    if not is_available_before_target(available_at, target_date):
        raise ValueError(
            f"Temporal leakage: available_at={available_at.isoformat()} "
            f"cutoff={target_cutoff(target_date).isoformat()}"
        )
