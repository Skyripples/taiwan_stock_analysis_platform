"""Build leakage-safe global features from PostgreSQL for TAIEX research.

This is a research-only pipeline.  It does not alter the production model,
calibrator, prediction output, or the disabled global feature registry.
"""

from __future__ import annotations

import bisect
import csv
import gzip
import json
import logging
import math
import os
import statistics
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import PROJECT_ROOT
from database.connection import connect
from database.global_market_repository import GlobalMarketRepository


LOGGER = logging.getLogger("global_feature_builder")
HISTORY = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
DATASET = PROJECT_ROOT / "data" / "analysis" / "global_taiex_feature_dataset.json.gz"
SUMMARY = PROJECT_ROOT / "data" / "analysis" / "global_feature_summary.json"
VERSION = "v3.13-phase3-1.0"
TAIPEI = ZoneInfo("Asia/Taipei")

FORMAL_FEATURES = (
    "taiex_close", "taiex_change_percent", "tpex_close", "turnover", "advancing",
    "declining", "foreign_cash_flow", "foreign_futures_position", "night_futures_change",
    "tsm_adr_change_percent", "sox_change_percent", "sp500_change_percent",
    "nasdaq_change_percent", "formal_vix_change_percent", "kospi_change_percent",
)
DAILY = {
    "NDX": "nasdaq100", "RUT": "russell2000", "EWT": "ewt", "VIX": "vix",
    "COPPER": "copper", "WTI": "wti", "GOLD": "gold", "USDJPY": "usdjpy",
    "USDKRW": "usdkrw",
}
BASE_KEYS = {
    "NDX": "nasdaq100_change_percent", "RUT": "russell2000_change_percent",
    "EWT": "ewt_change_percent", "VIX": "vix_change_percent",
    "COPPER": "copper_change_percent", "WTI": "wti_change_percent",
    "GOLD": "gold_change_percent", "USDJPY": "usdjpy_change_percent",
    "USDKRW": "usdkrw_change_percent",
}
RATE_KEYS = {"DGS5": "us5y_change", "DGS30": "us30y_change"}
CRYPTO = {"BTCUSD": (1, 4, 12, 24), "ETHUSD": (24,)}


def _cutoff(target: str) -> datetime:
    return datetime.combine(date.fromisoformat(target), time(9), TAIPEI)


def _load_labels(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        source_formal = tuple("vix_change_percent" if key == "formal_vix_change_percent" else key for key in FORMAL_FEATURES)
        required = {"feature_date", "target_date", "next_taiex_return", "target_direction", *source_formal}
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError("Historical dataset missing: " + ", ".join(sorted(missing)))
        rows = []
        for source in reader:
            row = {"feature_date": source["feature_date"], "target_date": source["target_date"]}
            for key in FORMAL_FEATURES:
                source_key = "vix_change_percent" if key == "formal_vix_change_percent" else key
                row[key] = float(source[source_key])
            row["next_taiex_return"] = float(source["next_taiex_return"])
            row["target_direction"] = int(source["target_direction"])
            rows.append(row)
    if any(rows[i]["feature_date"] >= rows[i + 1]["feature_date"] for i in range(len(rows) - 1)):
        raise ValueError("Historical labels are not strictly chronological")
    return rows


def _load_db(connection):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT mi.instrument_id, mi.canonical_symbol, p.trade_date, p.close,
                   p.adjusted_close, p.available_at
            FROM market_daily_prices p JOIN market_instruments mi USING (instrument_id)
            WHERE mi.canonical_symbol = ANY(%s) AND COALESCE(p.adjusted_close,p.close) IS NOT NULL
            ORDER BY mi.canonical_symbol,p.available_at
        """, (list(DAILY),))
        daily: dict[str, list[dict[str, Any]]] = {key: [] for key in DAILY}
        for row in cursor.fetchall():
            daily[row["canonical_symbol"]].append(dict(row))
        cursor.execute("""
            SELECT ms.series_key,o.observation_date,o.value,o.available_at
            FROM macro_observations o JOIN macro_series ms USING (macro_series_id)
            WHERE ms.series_key = ANY(%s) AND o.value IS NOT NULL
            ORDER BY ms.series_key,o.available_at
        """, (["DGS5", "DGS30", "T10Y3M", "ICSA"],))
        macro = {key: [] for key in ("DGS5", "DGS30", "T10Y3M", "ICSA")}
        for row in cursor.fetchall():
            macro[row["series_key"]].append(dict(row))
        cursor.execute("""
            SELECT mi.instrument_id,mi.canonical_symbol,p.timestamp_utc,p.close,p.available_at
            FROM market_intraday_prices p JOIN market_instruments mi USING(instrument_id)
            WHERE mi.canonical_symbol = ANY(%s) AND p.interval_seconds=3600 AND p.close IS NOT NULL
            ORDER BY mi.canonical_symbol,p.available_at
        """, (list(CRYPTO),))
        crypto = {key: [] for key in CRYPTO}
        for row in cursor.fetchall():
            crypto[row["canonical_symbol"]].append(dict(row))
    return daily, macro, crypto


def _eligible(items: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    positions = [item["available_at"] for item in items]
    return items[:bisect.bisect_left(positions, cutoff)]


def _std(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def _daily_features(symbol: str, items: list[dict[str, Any]], cutoff: datetime):
    eligible = _eligible(items, cutoff)
    if not eligible:
        return {}, {}
    closes = [float(row["adjusted_close"] if row["adjusted_close"] is not None else row["close"]) for row in eligible]
    latest = eligible[-1]
    prefix = DAILY[symbol]
    values: dict[str, float | None] = {}
    availability: dict[str, datetime] = {}
    if symbol == "VIX":
        values["vix_level"] = closes[-1]
        availability["vix_level"] = latest["available_at"]
    for days in (1, 3, 5):
        if len(closes) > days:
            key = BASE_KEYS[symbol] if days == 1 else f"{prefix}_return_{days}d"
            values[key] = (closes[-1] / closes[-1 - days] - 1) * 100
            availability[key] = latest["available_at"]
    returns = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
    for window in (5, 20):
        if len(returns) >= window:
            key = f"{prefix}_volatility_{window}d"
            values[key] = _std(returns[-window:])
            availability[key] = latest["available_at"]
    if len(closes) >= 20:
        sample = closes[-20:]
        deviation = _std(sample)
        values[f"{prefix}_zscore_20d"] = (closes[-1] - statistics.mean(sample)) / deviation if deviation else 0.0
        availability[f"{prefix}_zscore_20d"] = latest["available_at"]
    return values, availability


def _macro_features(macro: dict[str, list[dict[str, Any]]], cutoff: datetime):
    values: dict[str, float] = {}; availability: dict[str, datetime] = {}
    for series, key in RATE_KEYS.items():
        rows = _eligible(macro[series], cutoff)
        if len(rows) >= 2:
            values[key] = float(rows[-1]["value"]) - float(rows[-2]["value"])
            availability[key] = rows[-1]["available_at"]
    spreads = _eligible(macro["T10Y3M"], cutoff)
    if spreads:
        values["spread_10y_3m"] = float(spreads[-1]["value"]); availability["spread_10y_3m"] = spreads[-1]["available_at"]
    if len(spreads) >= 2:
        values["spread_10y_3m_change"] = float(spreads[-1]["value"]) - float(spreads[-2]["value"])
        availability["spread_10y_3m_change"] = spreads[-1]["available_at"]
    claims = _eligible(macro["ICSA"], cutoff)
    if claims:
        values["initial_claims_value"] = float(claims[-1]["value"]); availability["initial_claims_value"] = claims[-1]["available_at"]
    if len(claims) >= 2:
        values["initial_claims_change"] = float(claims[-1]["value"]) - float(claims[-2]["value"])
        availability["initial_claims_change"] = claims[-1]["available_at"]
    if len(claims) >= 53 and float(claims[-53]["value"]):
        values["initial_claims_yoy"] = (float(claims[-1]["value"]) / float(claims[-53]["value"]) - 1) * 100
        availability["initial_claims_yoy"] = claims[-1]["available_at"]
    return values, availability


def _crypto_features(crypto: dict[str, list[dict[str, Any]]], cutoff: datetime):
    values: dict[str, float] = {}; availability: dict[str, datetime] = {}
    for symbol, windows in CRYPTO.items():
        rows = _eligible(crypto[symbol], cutoff)
        if not rows:
            continue
        # Only completed hourly candles are eligible.  available_at is the candle end.
        latest = rows[-1]
        for hours in windows:
            if len(rows) > hours and float(rows[-1 - hours]["close"]):
                prefix = "btc" if symbol == "BTCUSD" else "eth"
                key = f"{prefix}_return_{hours}h"
                values[key] = (float(latest["close"]) / float(rows[-1 - hours]["close"]) - 1) * 100
                availability[key] = latest["available_at"]
    return values, availability


def _feature_summary(rows: list[dict[str, Any]], avail: list[dict[str, datetime]], feature_keys: list[str]):
    output = []
    total = len(rows)
    for key in feature_keys:
        present = [(row["target_date"], float(row[key]), a[key]) for row, a in zip(rows, avail) if row.get(key) is not None]
        values = [item[1] for item in present]
        before = sum(item[2] < _cutoff(item[0]) for item in present)
        output.append({
            "feature_key": key, "first_date": present[0][0] if present else None,
            "last_date": present[-1][0] if present else None, "observations": len(present),
            "missing_rate": round(1 - len(present) / total, 12),
            "mean": round(statistics.mean(values), 12) if values else None,
            "std": round(statistics.stdev(values), 12) if len(values) > 1 else None,
            "min": min(values) if values else None, "max": max(values) if values else None,
            "available_before_open_rate": round(before / len(present), 12) if present else None,
        })
    return output


def build() -> dict[str, Any]:
    labels = _load_labels(HISTORY)
    connection = connect()
    try:
        daily, macro, crypto = _load_db(connection)
        rows: list[dict[str, Any]] = []; availability_rows: list[dict[str, datetime]] = []; db_rows = []
        for label in labels:
            cutoff = _cutoff(label["target_date"])
            values: dict[str, Any] = dict(label); available: dict[str, datetime] = {}
            for symbol, items in daily.items():
                found, times = _daily_features(symbol, items, cutoff); values.update(found); available.update(times)
            found, times = _macro_features(macro, cutoff); values.update(found); available.update(times)
            found, times = _crypto_features(crypto, cutoff); values.update(found); available.update(times)
            for key, value in values.items():
                if key in label or value is None:
                    continue
                timestamp = available[key]
                if not timestamp < cutoff:
                    raise ValueError(f"Temporal leakage: {key} {label['target_date']} {timestamp} >= {cutoff}")
                db_rows.append({"feature_key": key, "target_date": label["target_date"], "target_scope": "TAIEX",
                    "cutoff_at": cutoff, "value": value, "available_at": timestamp,
                    "source_instrument_id": None, "transform_version": VERSION, "quality_flags": {},
                    "metadata": {"feature_date": label["feature_date"], "research_only": True}})
            rows.append(values); availability_rows.append(available)
        feature_keys = sorted(set().union(*(set(row) for row in rows)) - set(labels[0]))
        # The SELECTs above start psycopg's outer transaction.  Commit that same
        # transaction explicitly; a nested transaction context would only
        # release a savepoint and connection.close() would roll the outer one back.
        repository = GlobalMarketRepository(connection)
        try:
            for index in range(0, len(db_rows), 1000):
                repository.upsert_many("market_features", db_rows[index:index + 1000])
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        complete3_start = date.fromisoformat(labels[-1]["target_date"]).replace(year=date.fromisoformat(labels[-1]["target_date"]).year - 3)
        profiles = {}
        for name, start, keys in (
            ("complete_case_3y", complete3_start.isoformat(), feature_keys),
            ("complete_case_5y", (complete3_start.replace(year=complete3_start.year - 2)).isoformat(), feature_keys),
            ("long_history_subset", labels[0]["target_date"], [k for k in feature_keys if not k.startswith(("btc_", "eth_"))]),
        ):
            selected = [r for r in rows if r["target_date"] >= start and all(r.get(k) is not None for k in keys)]
            profiles[name] = {"requested_start": start, "actual_start": selected[0]["target_date"] if selected else None,
                "actual_end": selected[-1]["target_date"] if selected else None, "rows": len(selected),
                "feature_count": len(keys), "limited_by_taiex_labels": start < labels[0]["target_date"]}
        summary = {"generated_at": datetime.now(timezone.utc).isoformat(), "version": VERSION,
            "source": "PostgreSQL global market tables", "label_rows": len(labels),
            "label_range": {"first": labels[0]["target_date"], "last": labels[-1]["target_date"]},
            "feature_count": len(feature_keys), "features": _feature_summary(rows, availability_rows, feature_keys),
            "dataset_profiles": profiles,
            "formal_feature_aliases": {"formal_vix_change_percent": "production historical vix_change_percent"},
            "limitations": ["TAIEX labelled history starts in 2023; 5-year/long-history model profiles cannot be manufactured.",
                "ICSA values are current FRED observations with scheduled release timestamps, not point-in-time ALFRED vintages."],
            "rows_upserted": len(db_rows)}
        _write_gzip(DATASET, {"version": VERSION, "formal_features": list(FORMAL_FEATURES), "global_features": feature_keys, "rows": rows})
        _write_json(SUMMARY, summary)
        return summary
    finally:
        connection.close()


def _write_json(path: Path, payload: Any):
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False); handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def _write_gzip(path: Path, payload: Any):
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        report = build()
    except Exception as exc:
        LOGGER.error("Global feature build failed: %s", exc)
        return 1
    LOGGER.info("Global features built | labels=%d | features=%d | upserts=%d", report["label_rows"], report["feature_count"], report["rows_upserted"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
