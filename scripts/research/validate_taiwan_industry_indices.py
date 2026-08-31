"""Validate the local official Taiwan industry-index Parquet foundation."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import ssl
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests

from config import PROJECT_ROOT
from research.backfill_taiwan_industry_indices import (
    LAKE_ROOT,
    MANIFEST_PATH,
    REGISTRY_PATH,
    VALIDATION_PATH,
    OfficialClient,
    _as_date,
    fetch_twse_history,
    fetch_tpex_history,
    load_config,
    load_existing_rows,
)


def validate(sample_size: int = 5) -> dict[str, Any]:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = load_existing_rows()
    if not rows:
        raise ValueError("Industry-index Data Lake is empty")

    keys = [(str(row["market"]), str(row["industry"]), _as_date(row["trade_date"])) for row in rows]
    duplicates = len(keys) - len(set(keys))
    if duplicates:
        raise ValueError(f"Duplicate industry-index primary keys: {duplicates}")

    invalid_ohlc = 0
    invalid_change = 0
    invalid_timezone = 0
    nullable_counts: Counter[str] = Counter()
    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_dataset[f"{row['market']}:{row['industry']}"].append(row)
        close = float(row["close"])
        if not math.isfinite(close) or close <= 0:
            invalid_ohlc += 1
        for field in ("open", "high", "low"):
            if row[field] is None:
                nullable_counts[field] += 1
            elif not math.isfinite(float(row[field])) or float(row[field]) <= 0:
                invalid_ohlc += 1
        if row["open"] is not None and row["high"] is not None and row["low"] is not None:
            if float(row["high"]) < max(float(row["open"]), close) or float(row["low"]) > min(float(row["open"]), close):
                invalid_ohlc += 1
        if row["change"] is None or row["change_percent"] is None:
            nullable_counts["change"] += row["change"] is None
            nullable_counts["change_percent"] += row["change_percent"] is None
        else:
            change = float(row["change"])
            previous_close = close - change
            if previous_close <= 0:
                invalid_change += 1
            else:
                recomputed = change / previous_close * 100
                if abs(recomputed - float(row["change_percent"])) > 0.025:
                    invalid_change += 1
        if not str(row["available_at"]).endswith("+08:00"):
            invalid_timezone += 1
        if row["trading_value"] is None:
            nullable_counts["trading_value"] += 1

    ordering_failures = 0
    dataset_profiles: dict[str, dict[str, Any]] = {}
    for key, items in sorted(by_dataset.items()):
        dates = [_as_date(row["trade_date"]) for row in items]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            ordering_failures += 1
        dataset_profiles[key] = {"rows": len(dates), "first_date": min(dates).isoformat(), "last_date": max(dates).isoformat()}

    official_checks = _official_spot_checks(rows, registry, sample_size)
    tpex_audit = _audit_tpex_official_catalog()
    registry_names = {
        (market, item["industry"])
        for market in ("twse", "tpex")
        for item in registry["markets"][market]["datasets"]
    }
    stored_names = {(str(row["market"]), str(row["industry"])) for row in rows}
    missing_registry_datasets = sorted(registry_names - stored_names)
    report = {
        "version": "1.0",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "passed" if not any((duplicates, invalid_ohlc, invalid_change, invalid_timezone, ordering_failures, len(missing_registry_datasets))) else "failed",
        "registry": {
            "twse": registry["markets"]["twse"]["industry_index_count"],
            "tpex": registry["markets"]["tpex"]["industry_index_count"],
            "missing_stored_datasets": missing_registry_datasets,
            "tpex_official_catalog_audit": tpex_audit,
        },
        "storage": {
            "rows": len(rows),
            "files": manifest["files"],
            "bytes": manifest["bytes"],
            "first_date": min(_as_date(row["trade_date"]) for row in rows).isoformat(),
            "last_date": max(_as_date(row["trade_date"]) for row in rows).isoformat(),
            "datasets": dataset_profiles,
        },
        "quality": {
            "pk_duplicates": duplicates,
            "ordering_failures": ordering_failures,
            "invalid_ohlc": invalid_ohlc,
            "invalid_change_or_percent": invalid_change,
            "invalid_timezone": invalid_timezone,
            "null_counts": dict(nullable_counts),
            "official_spot_checks": official_checks,
        },
    }
    _write_atomic(VALIDATION_PATH, report)
    if report["status"] != "passed":
        raise ValueError("Industry-index validation failed")
    return report


def _official_spot_checks(rows: list[dict[str, Any]], registry: dict[str, Any], sample_size: int) -> list[dict[str, Any]]:
    rng = random.Random(315)
    twse_candidates = [row for row in rows if row["market"] == "twse"]
    tpex_candidates = [row for row in rows if row["market"] == "tpex"]
    tpex_size = min(2, sample_size, len(tpex_candidates))
    twse_size = min(sample_size - tpex_size, len(twse_candidates))
    selected = rng.sample(twse_candidates, twse_size) + rng.sample(tpex_candidates, tpex_size)
    client = OfficialClient(delay=0.05)
    by_market_name = {
        (market, item["industry"]): item
        for market in ("twse", "tpex")
        for item in registry["markets"][market]["datasets"]
    }
    checks = []
    for row in selected:
        trade_date = _as_date(row["trade_date"])
        dataset = by_market_name[(str(row["market"]), str(row["industry"]))]
        observations = (
            fetch_twse_history(client, dataset, trade_date, trade_date)
            if row["market"] == "twse"
            else fetch_tpex_history(client, [dataset], trade_date, trade_date)
        )
        if len(observations) != 1:
            raise ValueError(f"Official spot check returned {len(observations)} rows")
        official = observations[0]
        differences = {
            field: abs(float(row[field]) - float(official[field]))
            for field in ("close", "change", "change_percent")
        }
        passed = differences["close"] < 1e-9 and differences["change"] < 1e-9 and differences["change_percent"] < 1e-9
        if not passed:
            raise ValueError(f"Official spot check failed: {row['industry']} {trade_date}")
        checks.append({"market": row["market"], "industry": row["industry"], "trade_date": trade_date.isoformat(), "passed": True, "differences": differences})
    return checks


def _audit_tpex_official_catalog() -> dict[str, Any]:
    source = "https://www.tpex.org.tw/openapi/swagger.json"
    context = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    request = urllib.request.Request(source, headers={"User-Agent": "taiwan-stock-analysis-platform/3.15"})
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        paths = json.loads(response.read().decode("utf-8")).get("paths", {})
    historical_indices = []
    industry_index_paths = []
    for path, methods in paths.items():
        text = json.dumps(methods, ensure_ascii=False)
        if "指數" in text and "歷史" in text:
            historical_indices.append(path)
        if "產業" in text and "指數" in text:
            industry_index_paths.append(path)
    return {
        "source": source,
        "historical_index_paths": sorted(historical_indices),
        "official_industry_index_paths": sorted(industry_index_paths),
        "monthly_industry_index_endpoint": "https://www.tpex.org.tw/www/zh-tw/indexInfo/idxsm",
        "conclusion": "official monthly query provides daily industry-index closes",
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=5)
    args = parser.parse_args()
    try:
        report = validate(args.sample_size)
    except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
        print(f"Industry-index validation failed: {exc}")
        return 1
    print(json.dumps({"status": report["status"], "rows": report["storage"]["rows"], "twse": report["registry"]["twse"], "tpex": report["registry"]["tpex"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
