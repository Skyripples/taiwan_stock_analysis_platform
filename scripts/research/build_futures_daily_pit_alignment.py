"""Build local, PIT-safe TX daily prediction features without targets or ML."""

from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import json
import os
import statistics
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from config import PROJECT_ROOT
from trading_calendar import get_trading_days_in_range


UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")
VERSION = "v4.6-1.2"
DEFAULT_START = date(2021, 10, 14)
TX_ROOT = PROJECT_ROOT / "data_lake" / "staging" / "futures_daily" / "product=TX"
GLOBAL_ROOT = PROJECT_ROOT / "data_lake" / "global_market"
INSTITUTIONAL_ROOT = PROJECT_ROOT / "data_lake" / "futures_institutional" / "daily_positions"
PCR_ROOT = PROJECT_ROOT / "data_lake" / "futures_options" / "daily_pcr"
OUTPUT_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "daily_features"
LOCAL_HISTORY_PATHS = (
    PROJECT_ROOT / "data" / "history" / "backfill_market_daily.csv",
    PROJECT_ROOT / "data" / "history" / "market_daily.csv",
)
MARKET_FACTORS = {
    "SOX": "sox",
    "TSM_ADR": "tsm_adr",
    "NASDAQ": "nasdaq",
    "SP500": "sp500",
    "USDTWD": "usdtwd",
}
MACRO_FACTORS = {"DGS10": "dgs10"}
INSTITUTIONAL_INVESTORS = {
    "外資": "foreign",
    "投信": "investment_trust",
    "自營商": "dealer",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build TX daily PIT-aligned local features")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=lambda value: date.today() if value == "today" else date.fromisoformat(value), default=date.today())
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    options = parser.parse_args()
    if options.start > options.end:
        parser.error("--start cannot exceed --end")
    return options


def aware_datetime(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Timestamp must be timezone-aware: {value}")
    return parsed


def prediction_timestamp(day: date) -> datetime:
    return datetime.combine(day, time(8, 30), TAIPEI)


def read_tx_daily(root: Path = TX_ROOT) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("year=*/daily.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row.get("product") == "TX" and row.get("session") == "regular" and row.get("contract_rank") in {"front", "next"}:
                    rows.append(row)
    return rows


def optional_number(value: Any) -> float | None:
    try:
        return float(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def conservative_local_available_at(source_date: date) -> datetime:
    """Local daily snapshots are only eligible from midnight after source day."""
    return datetime.combine(source_date + timedelta(days=1), time(0, 0), TAIPEI).astimezone(UTC)


def read_local_factors(paths: Iterable[Path] = LOCAL_HISTORY_PATHS) -> dict[str, list[dict[str, Any]]]:
    """Read existing TAIEX and foreign TX net-OI history without downloading data."""
    collected: dict[str, dict[date, dict[str, Any]]] = {"TAIEX_SPOT": {}, "FOREIGN_FUTURES_NET_OI": {}}
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                definitions = (
                    ("TAIEX_SPOT", "taiex_close", "taiwan_market_trade_date"),
                    ("FOREIGN_FUTURES_NET_OI", "foreign_futures_position", "foreign_futures_trade_date"),
                )
                for key, value_field, date_field in definitions:
                    value = optional_number(row.get(value_field))
                    raw_date = row.get(date_field)
                    if value is None or not raw_date:
                        continue
                    source_date = date.fromisoformat(raw_date[:10])
                    collected[key][source_date] = {
                        "value": value, "source_date": source_date,
                        "available_at": conservative_local_available_at(source_date),
                    }
    return {key: sorted(rows.values(), key=lambda row: row["available_at"]) for key, rows in collected.items()}


def read_external_factors(root: Path = GLOBAL_ROOT) -> dict[str, list[dict[str, Any]]]:
    import pyarrow.parquet as pq

    output = {key: [] for key in (*MARKET_FACTORS, *MACRO_FACTORS)}
    for path in (root / "daily_prices").rglob("*.parquet"):
        for row in pq.ParquetFile(path).read().to_pylist():
            symbol = row.get("canonical_symbol")
            if symbol in MARKET_FACTORS:
                output[symbol].append({"value": row.get("close"), "source_date": row["trade_date"],
                    "available_at": aware_datetime(row["available_at"]).astimezone(UTC)})
    for path in (root / "macro_observations" / "series_key=DGS10").rglob("*.parquet"):
        for row in pq.ParquetFile(path).read().to_pylist():
            output["DGS10"].append({"value": row.get("value"), "source_date": row["observation_date"],
                "available_at": aware_datetime(row["available_at"]).astimezone(UTC)})
    for rows in output.values():
        rows.sort(key=lambda row: row["available_at"])
    return output


def read_institutional_positions(root: Path = INSTITUTIONAL_ROOT) -> dict[str, list[dict[str, Any]]]:
    """Read canonical TAIFEX TX institutional OI without any network access."""
    import pyarrow.parquet as pq

    output = {investor: [] for investor in INSTITUTIONAL_INVESTORS}
    keys: set[tuple[date, str]] = set()
    for path in sorted(root.glob("partition_year=*/part-00000.parquet")):
        for row in pq.ParquetFile(path).read().to_pylist():
            investor = row.get("investor_type")
            if investor not in INSTITUTIONAL_INVESTORS:
                continue
            source_date = row["trade_date"]
            key = (source_date, investor)
            if key in keys:
                raise ValueError(f"Duplicate institutional canonical key: {source_date} {investor}")
            keys.add(key)
            output[investor].append({
                "source_date": source_date,
                "available_at": aware_datetime(row["available_at"]).astimezone(UTC),
                "long_open_interest": row.get("long_open_interest"),
                "short_open_interest": row.get("short_open_interest"),
                "net_open_interest": row.get("net_open_interest"),
            })
    for rows in output.values():
        rows.sort(key=lambda row: row["available_at"])
    return output


def read_pcr_observations(root: Path = PCR_ROOT) -> list[dict[str, Any]]:
    """Read local canonical TXO PCR observations with their PIT availability."""
    import pyarrow.parquet as pq

    output: list[dict[str, Any]] = []
    seen: set[date] = set()
    for path in sorted(root.glob("partition_year=*/part-00000.parquet")):
        for row in pq.ParquetFile(path).read().to_pylist():
            trade_date = row["trade_date"]
            if trade_date in seen:
                raise ValueError(f"Duplicate PCR canonical trade_date: {trade_date}")
            seen.add(trade_date)
            output.append({
                "source_date": trade_date,
                "available_at": aware_datetime(row["available_at"]).astimezone(UTC),
                "put_volume": row.get("put_volume"), "call_volume": row.get("call_volume"),
                "volume_pcr": row.get("volume_pcr"), "put_oi": row.get("put_oi"),
                "call_oi": row.get("call_oi"), "oi_pcr": row.get("oi_pcr"),
                "oi_available": bool(row.get("oi_available")),
            })
    output.sort(key=lambda row: row["available_at"])
    return output


def latest_available(rows: list[dict[str, Any]], prediction_at: datetime) -> dict[str, Any] | None:
    cutoff = prediction_at.astimezone(UTC)
    timestamps = [row["available_at"] for row in rows]
    index = bisect.bisect_right(timestamps, cutoff) - 1
    return rows[index] if index >= 0 else None


def latest_available_pair(
    rows: list[dict[str, Any]], prediction_at: datetime,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the latest eligible row and its preceding eligible observation."""
    cutoff = prediction_at.astimezone(UTC)
    timestamps = [row["available_at"] for row in rows]
    index = bisect.bisect_right(timestamps, cutoff) - 1
    if index < 0:
        return None, None
    return rows[index], rows[index - 1] if index else None


def tx_by_date(rows: Iterable[dict[str, Any]], rank: str = "front") -> dict[date, dict[str, Any]]:
    output: dict[date, dict[str, Any]] = {}
    for row in rows:
        if row.get("product") != "TX" or row.get("session") != "regular" or row.get("contract_rank") != rank:
            continue
        day = date.fromisoformat(str(row["trade_date"])[:10])
        if day in output:
            raise ValueError(f"Duplicate TX {rank} regular row: {day.isoformat()}")
        output[day] = row
    return output


def ratio_change(current: Any, previous: Any) -> float | None:
    current_value, previous_value = optional_number(current), optional_number(previous)
    return current_value / previous_value - 1 if current_value is not None and previous_value not in {None, 0} else None


def difference(current: Any, previous: Any) -> float | None:
    current_value, previous_value = optional_number(current), optional_number(previous)
    return current_value - previous_value if current_value is not None and previous_value is not None else None


def same_contract_return(current: dict[str, Any] | None, previous: dict[str, Any] | None) -> float | None:
    if not current or not previous or current.get("contract") != previous.get("contract"):
        return None
    return ratio_change(current.get("close"), previous.get("close"))


def trailing_return_volatility(sessions: list[date], index: int, tx: dict[date, dict[str, Any]], window: int = 5) -> float | None:
    values: list[float] = []
    for cursor in range(max(1, index - window + 1), index + 1):
        value = same_contract_return(tx.get(sessions[cursor]), tx.get(sessions[cursor - 1]))
        if value is None:
            return None
        values.append(value)
    return statistics.pstdev(values) if len(values) == window else None


def align_rows(
    sessions: list[date], output_start: date, output_end: date,
    tx_rows: Iterable[dict[str, Any]], factors: dict[str, list[dict[str, Any]]],
    local_factors: dict[str, list[dict[str, Any]]] | None = None,
    institutional_positions: dict[str, list[dict[str, Any]]] | None = None,
    pcr_observations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    tx_rows = list(tx_rows)
    tx = tx_by_date(tx_rows, "front")
    tx_next = tx_by_date(tx_rows, "next")
    local_factors = local_factors or {"TAIEX_SPOT": [], "FOREIGN_FUTURES_NET_OI": []}
    institutional_positions = institutional_positions or {investor: [] for investor in INSTITUTIONAL_INVESTORS}
    pcr_observations = pcr_observations or []
    session_index = {day: index for index, day in enumerate(sessions)}
    result: list[dict[str, Any]] = []
    for prediction_date in sessions:
        if not output_start <= prediction_date <= output_end:
            continue
        index = session_index[prediction_date]
        if index == 0:
            continue
        source_date = sessions[index - 1]
        anchor = tx.get(source_date)
        if anchor is None:
            continue
        prediction_at = prediction_timestamp(prediction_date)
        tx_available = aware_datetime(anchor["available_at"])
        if tx_available > prediction_at:
            raise ValueError(f"TX anchor is not PIT-safe: {source_date} -> {prediction_date}")

        flags: dict[str, Any] = {
            "missing_external_factors": [], "missing_local_factors": [], "derived_feature_gaps": [],
            "missing_institutional_factors": [],
            "missing_pcr_volume": False, "missing_pcr_oi": False,
            "tx_roll_regime": False, "tx_roll_reasons": [],
        }
        days_to_expiry = anchor.get("days_to_expiry")
        if anchor.get("is_expiry_week") or (isinstance(days_to_expiry, int) and 0 <= days_to_expiry <= 7):
            flags["tx_roll_regime"] = True
            flags["tx_roll_reasons"].append("expiry_week")
        prior_anchor = tx.get(sessions[index - 2]) if index >= 2 else None
        if prior_anchor and prior_anchor.get("contract") != anchor.get("contract"):
            flags["tx_roll_regime"] = True
            flags["tx_roll_reasons"].append("front_contract_changed")
        if anchor.get("roll_date") == source_date.isoformat():
            flags["tx_roll_regime"] = True
            flags["tx_roll_reasons"].append("roll_date")

        row: dict[str, Any] = {
            "partition_year": prediction_date.year, "prediction_date": prediction_date,
            "prediction_at": prediction_at, "feature_version": VERSION,
            "tx_source_trading_date": source_date, "tx_contract": anchor.get("contract"),
            "tx_open": anchor.get("open"), "tx_high": anchor.get("high"), "tx_low": anchor.get("low"),
            "tx_close": anchor.get("close"), "tx_volume": anchor.get("volume"),
            "tx_open_interest": anchor.get("open_interest"), "tx_days_to_expiry": days_to_expiry,
            "tx_expiry_week": bool(anchor.get("is_expiry_week") or (isinstance(days_to_expiry, int) and 0 <= days_to_expiry <= 7)),
            "tx_roll_regime": flags["tx_roll_regime"],
            "tx_available_at": tx_available.astimezone(UTC),
        }
        availabilities = [tx_available.astimezone(UTC)]

        same_contract = bool(prior_anchor and prior_anchor.get("contract") == anchor.get("contract"))
        row["tx_prior_source_trading_date"] = date.fromisoformat(str(prior_anchor["trade_date"])[:10]) if prior_anchor else None
        row["tx_prior_available_at"] = aware_datetime(prior_anchor["available_at"]).astimezone(UTC) if prior_anchor else None
        if row["tx_prior_available_at"] is not None:
            if row["tx_prior_available_at"] > prediction_at.astimezone(UTC):
                raise ValueError(f"Prior TX row is not PIT-safe: {source_date} -> {prediction_date}")
            availabilities.append(row["tx_prior_available_at"])
        row["tx_previous_day_return"] = same_contract_return(anchor, prior_anchor)
        row["tx_open_to_close_return"] = ratio_change(anchor.get("close"), anchor.get("open"))
        high, low, open_value = (optional_number(anchor.get(key)) for key in ("high", "low", "open"))
        row["tx_intraday_range_pct"] = (high - low) / open_value if high is not None and low is not None and open_value not in {None, 0} else None
        if same_contract:
            prior_close = optional_number(prior_anchor.get("close"))
            row["tx_true_range_pct"] = max(high - low, abs(high - prior_close), abs(low - prior_close)) / prior_close if high is not None and low is not None and prior_close not in {None, 0} else None
            row["tx_volume_change_1d"] = ratio_change(anchor.get("volume"), prior_anchor.get("volume"))
            row["tx_open_interest_change_1d"] = difference(anchor.get("open_interest"), prior_anchor.get("open_interest"))
            row["tx_open_interest_change_pct"] = ratio_change(anchor.get("open_interest"), prior_anchor.get("open_interest"))
        else:
            row["tx_true_range_pct"] = None
            row["tx_volume_change_1d"] = None
            row["tx_open_interest_change_1d"] = None
            row["tx_open_interest_change_pct"] = None
            flags["derived_feature_gaps"].append("prior_same_contract")
        row["tx_return_volatility_5d"] = trailing_return_volatility(sessions, index - 1, tx)
        if row["tx_return_volatility_5d"] is None:
            flags["derived_feature_gaps"].append("return_volatility_5d")

        next_contract = tx_next.get(source_date)
        next_available = aware_datetime(next_contract["available_at"]).astimezone(UTC) if next_contract else None
        if next_contract and next_available <= prediction_at.astimezone(UTC):
            row["tx_next_contract"] = next_contract.get("contract")
            row["tx_next_available_at"] = next_available
            row["tx_front_next_spread"] = difference(next_contract.get("close"), anchor.get("close"))
            row["tx_front_next_spread_pct"] = ratio_change(next_contract.get("close"), anchor.get("close"))
            availabilities.append(next_available)
        else:
            row["tx_next_contract"] = None
            row["tx_next_available_at"] = None
            row["tx_front_next_spread"] = None
            row["tx_front_next_spread_pct"] = None
            flags["derived_feature_gaps"].append("front_next_spread")

        spot = latest_available(local_factors.get("TAIEX_SPOT", []), prediction_at)
        if spot and spot.get("source_date") == source_date:
            spot_available = aware_datetime(spot["available_at"]).astimezone(UTC)
            row["taiex_spot_value"] = spot.get("value")
            row["taiex_spot_source_date"] = spot.get("source_date")
            row["taiex_spot_available_at"] = spot_available
            row["tx_basis"] = difference(anchor.get("close"), spot.get("value"))
            row["tx_basis_pct"] = ratio_change(anchor.get("close"), spot.get("value"))
            availabilities.append(spot_available)
        else:
            row.update({"taiex_spot_value": None, "taiex_spot_source_date": None, "taiex_spot_available_at": None,
                        "tx_basis": None, "tx_basis_pct": None})
            flags["missing_local_factors"].append("TAIEX_SPOT")

        foreign = latest_available(local_factors.get("FOREIGN_FUTURES_NET_OI", []), prediction_at)
        if foreign:
            foreign_available = aware_datetime(foreign["available_at"]).astimezone(UTC)
            row["foreign_futures_net_oi"] = foreign.get("value")
            row["foreign_futures_source_date"] = foreign.get("source_date")
            row["foreign_futures_available_at"] = foreign_available
            availabilities.append(foreign_available)
        else:
            row["foreign_futures_net_oi"] = None
            row["foreign_futures_source_date"] = None
            row["foreign_futures_available_at"] = None
            flags["missing_local_factors"].append("FOREIGN_FUTURES_NET_OI")

        for investor_type, prefix in INSTITUTIONAL_INVESTORS.items():
            selected, previous = latest_available_pair(institutional_positions.get(investor_type, []), prediction_at)
            if selected is None:
                for metric in ("long_oi", "short_oi", "net_oi", "net_oi_change"):
                    row[f"{prefix}_{metric}"] = None
                row[f"{prefix}_source_date"] = None
                row[f"{prefix}_available_at"] = None
                flags["missing_institutional_factors"].append(investor_type)
                continue
            available_at = aware_datetime(selected["available_at"]).astimezone(UTC)
            if available_at > prediction_at.astimezone(UTC):
                raise ValueError(f"Institutional position is not PIT-safe: {investor_type} {prediction_date}")
            row[f"{prefix}_long_oi"] = selected.get("long_open_interest")
            row[f"{prefix}_short_oi"] = selected.get("short_open_interest")
            row[f"{prefix}_net_oi"] = selected.get("net_open_interest")
            row[f"{prefix}_net_oi_change"] = difference(
                selected.get("net_open_interest"), previous.get("net_open_interest") if previous else None,
            )
            row[f"{prefix}_source_date"] = selected.get("source_date")
            row[f"{prefix}_available_at"] = available_at
            availabilities.append(available_at)

        pcr = latest_available(pcr_observations, prediction_at)
        if pcr is None:
            row.update({
                "put_volume": None, "call_volume": None, "volume_pcr": None,
                "put_oi": None, "call_oi": None, "oi_pcr": None,
                "pcr_source_date": None, "pcr_available_at": None,
            })
            flags["missing_pcr_volume"] = True
            flags["missing_pcr_oi"] = True
        else:
            pcr_available = aware_datetime(pcr["available_at"]).astimezone(UTC)
            if pcr_available > prediction_at.astimezone(UTC):
                raise ValueError(f"PCR observation is not PIT-safe: {prediction_date}")
            row["put_volume"] = pcr.get("put_volume")
            row["call_volume"] = pcr.get("call_volume")
            row["volume_pcr"] = pcr.get("volume_pcr")
            row["pcr_source_date"] = pcr.get("source_date")
            row["pcr_available_at"] = pcr_available
            availabilities.append(pcr_available)
            if all(row[key] is not None for key in ("put_volume", "call_volume", "volume_pcr")):
                flags["missing_pcr_volume"] = False
            else:
                flags["missing_pcr_volume"] = True
            if pcr.get("oi_available"):
                row["put_oi"] = pcr.get("put_oi")
                row["call_oi"] = pcr.get("call_oi")
                row["oi_pcr"] = pcr.get("oi_pcr")
                flags["missing_pcr_oi"] = any(row[key] is None for key in ("put_oi", "call_oi", "oi_pcr"))
            else:
                row["put_oi"] = None
                row["call_oi"] = None
                row["oi_pcr"] = None
                flags["missing_pcr_oi"] = True
        for factor_id, prefix in {**MARKET_FACTORS, **MACRO_FACTORS}.items():
            selected = latest_available(factors.get(factor_id, []), prediction_at)
            if selected is None:
                row[f"{prefix}_value"] = None
                row[f"{prefix}_source_date"] = None
                row[f"{prefix}_available_at"] = None
                flags["missing_external_factors"].append(factor_id)
                continue
            available_at = aware_datetime(selected["available_at"]).astimezone(UTC)
            if available_at > prediction_at.astimezone(UTC):
                raise ValueError(f"External factor is not PIT-safe: {factor_id} {prediction_date}")
            row[f"{prefix}_value"] = selected.get("value")
            row[f"{prefix}_source_date"] = selected.get("source_date")
            row[f"{prefix}_available_at"] = available_at
            availabilities.append(available_at)
        row["max_feature_available_at"] = max(availabilities)
        row["quality_flags"] = json.dumps(flags, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        result.append(row)
    keys = [row["prediction_date"] for row in result]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate prediction_date in aligned output")
    return result


def output_schema() -> Any:
    import pyarrow as pa

    utc = pa.timestamp("us", tz="UTC")
    taipei = pa.timestamp("us", tz="Asia/Taipei")
    fields = [
        ("partition_year", pa.int64()), ("prediction_date", pa.date32()), ("prediction_at", taipei),
        ("feature_version", pa.string()), ("tx_source_trading_date", pa.date32()), ("tx_contract", pa.string()),
        ("tx_open", pa.float64()), ("tx_high", pa.float64()), ("tx_low", pa.float64()), ("tx_close", pa.float64()),
        ("tx_volume", pa.int64()), ("tx_open_interest", pa.int64()), ("tx_days_to_expiry", pa.int64()),
        ("tx_expiry_week", pa.bool_()), ("tx_roll_regime", pa.bool_()), ("tx_available_at", utc),
        ("tx_prior_source_trading_date", pa.date32()), ("tx_prior_available_at", utc),
        ("tx_previous_day_return", pa.float64()), ("tx_open_to_close_return", pa.float64()),
        ("tx_intraday_range_pct", pa.float64()), ("tx_true_range_pct", pa.float64()),
        ("tx_volume_change_1d", pa.float64()), ("tx_open_interest_change_1d", pa.float64()),
        ("tx_open_interest_change_pct", pa.float64()), ("tx_return_volatility_5d", pa.float64()),
        ("tx_next_contract", pa.string()), ("tx_next_available_at", utc),
        ("tx_front_next_spread", pa.float64()), ("tx_front_next_spread_pct", pa.float64()),
        ("taiex_spot_value", pa.float64()), ("taiex_spot_source_date", pa.date32()),
        ("taiex_spot_available_at", utc), ("tx_basis", pa.float64()), ("tx_basis_pct", pa.float64()),
        ("foreign_futures_net_oi", pa.float64()), ("foreign_futures_source_date", pa.date32()),
        ("foreign_futures_available_at", utc),
        ("put_volume", pa.int64()), ("call_volume", pa.int64()), ("volume_pcr", pa.float64()),
        ("put_oi", pa.int64()), ("call_oi", pa.int64()), ("oi_pcr", pa.float64()),
        ("pcr_source_date", pa.date32()), ("pcr_available_at", utc),
    ]
    for prefix in INSTITUTIONAL_INVESTORS.values():
        fields.extend([
            (f"{prefix}_long_oi", pa.float64()), (f"{prefix}_short_oi", pa.float64()),
            (f"{prefix}_net_oi", pa.float64()), (f"{prefix}_net_oi_change", pa.float64()),
            (f"{prefix}_source_date", pa.date32()), (f"{prefix}_available_at", utc),
        ])
    for prefix in (*MARKET_FACTORS.values(), *MACRO_FACTORS.values()):
        fields.extend([(f"{prefix}_value", pa.float64()), (f"{prefix}_source_date", pa.date32()),
                       (f"{prefix}_available_at", utc)])
    fields.extend([("max_feature_available_at", utc), ("quality_flags", pa.string())])
    return pa.schema(fields)


def parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).read().to_pylist()


def write_rows(rows: list[dict[str, Any]], root: Path) -> dict[str, int]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["prediction_date"].year, []).append(row)
    files = 0
    for year, additions in sorted(groups.items()):
        path = root / f"partition_year={year}" / "part-00000.parquet"
        merged = {row["prediction_date"]: row for row in parquet_rows(path)}
        merged.update({row["prediction_date"]: row for row in additions})
        ordered = [merged[key] for key in sorted(merged)]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            table = pa.Table.from_pylist(ordered, schema=output_schema())
            pq.write_table(table, temporary, compression="zstd", compression_level=6,
                           use_dictionary=["feature_version", "tx_contract", "quality_flags"])
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        files += 1
    return {"rows": len(rows), "files": files}


def run(options: argparse.Namespace) -> dict[str, Any]:
    calendar_start = options.start - timedelta(days=14)
    sessions, _ = get_trading_days_in_range(calendar_start, options.end)
    aligned = align_rows(
        sessions, options.start, options.end, read_tx_daily(), read_external_factors(), read_local_factors(),
        read_institutional_positions(), read_pcr_observations(),
    )
    stats = write_rows(aligned, options.output)
    return {"version": VERSION, "start": options.start.isoformat(), "end": options.end.isoformat(),
            "first_prediction_date": aligned[0]["prediction_date"].isoformat() if aligned else None,
            "last_prediction_date": aligned[-1]["prediction_date"].isoformat() if aligned else None, **stats}


def main() -> int:
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
