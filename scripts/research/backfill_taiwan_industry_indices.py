"""Incrementally backfill official Taiwan industry indices into the local lake."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from scripts.config import PROJECT_ROOT
from scripts.collectors import CollectorPolicy, OfficialHttpClient, StructuredLog


LOGGER = logging.getLogger("taiwan_industry_indices")
CONFIG_PATH = PROJECT_ROOT / "config" / "taiwan_industry_index_registry.json"
LAKE_ROOT = PROJECT_ROOT / "data_lake" / "taiwan_industry_indices"
REGISTRY_PATH = LAKE_ROOT / "registry.json"
MANIFEST_PATH = LAKE_ROOT / "manifest.json"
CHECKPOINT_PATH = LAKE_ROOT / "checkpoint.json"
GAPS_PATH = LAKE_ROOT / "official_gaps.json"
COVERAGE_PATH = LAKE_ROOT / "coverage.json"
VALIDATION_PATH = LAKE_ROOT / "validation_report.json"
SUMMARY_PATH = LAKE_ROOT / "summary.json"
STRUCTURED_LOG_PATH = LAKE_ROOT / "logs" / "collector.jsonl"
FRAMEWORK_CONFIG_PATH = PROJECT_ROOT / "config" / "collector_framework.json"
STOCK_PRICE_GLOB = PROJECT_ROOT / "data_lake" / "stock_daily_prices" / "**" / "*.parquet"
TAIPEI = ZoneInfo("Asia/Taipei")
USER_AGENT = "taiwan-stock-analysis-platform/3.15 industry-index-research"
INDUSTRY_SUFFIX = "類指數"


@dataclass(frozen=True)
class RunOptions:
    start: date
    end: date
    markets: tuple[str, ...]
    industry: str | None
    refresh: bool
    resume: bool
    workers: int
    batch_size: int
    batch_cooldown: float


class OfficialClient(OfficialHttpClient):
    """Industry adapter over the shared collector transport."""

    def __init__(self) -> None:
        framework = json.loads(FRAMEWORK_CONFIG_PATH.read_text(encoding="utf-8"))
        super().__init__(CollectorPolicy.from_config(framework["defaults"]), USER_AGENT, StructuredLog(STRUCTURED_LOG_PATH))

    def get_json(self, url: str | list[str], params: dict[str, str] | None = None) -> Any:
        return self.request_json("GET", url, params=params)

    def post_json(self, url: str | list[str], data: dict[str, str]) -> Any:
        return self.request_json("POST", url, data=data)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("timezone") != "Asia/Taipei":
        raise ValueError("Industry-index timezone must be Asia/Taipei")
    years = payload.get("history_years")
    if isinstance(years, bool) or not isinstance(years, int) or years <= 0:
        raise ValueError("history_years must be a positive integer")
    return payload


def discover_twse_registry(client: OfficialClient, config: dict[str, Any], end: date) -> list[dict[str, Any]]:
    source = config["markets"]["twse"]["source"]
    source_candidates = [source, *config["markets"]["twse"].get("official_mirrors", [])]
    payload: dict[str, Any] | None = None
    discovery_date: date | None = None
    for offset in range(0, 20):
        candidate = end - timedelta(days=offset)
        if candidate.weekday() >= 5:
            continue
        result = client.get_json(source_candidates, {"date": candidate.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
        if result.get("stat") == "OK" and result.get("tables"):
            payload = result
            discovery_date = candidate
            break
    if payload is None or discovery_date is None:
        raise RuntimeError("Unable to discover the TWSE industry-index registry")
    table = _price_index_table(payload)
    official_rows = {str(row[0]).strip(): row for row in table["data"] if str(row[0]).strip().endswith(INDUSTRY_SUFFIX)}
    names = sorted(official_rows)
    if not names:
        raise ValueError("TWSE discovery returned no official industry indices")
    history_registry_source = config["markets"]["twse"]["history_registry_source"]
    history_source_template = config["markets"]["twse"]["history_source_template"]
    history_indices = client.get_json(history_registry_source)
    if not isinstance(history_indices, list):
        raise ValueError("Taiwan Index Plus registry format changed")
    codes: dict[str, str] = {}
    for name, raw in official_rows.items():
        close = _number(raw[1])
        points = abs(_number(raw[3]))
        percentage = abs(_number(raw[4]))
        matches = []
        for item in history_indices:
            try:
                if (
                    abs(_number(item.get("index")) - close) < 0.011
                    and abs(abs(_number(item.get("volatility_points"))) - points) < 0.011
                    and abs(abs(_number(item.get("volatility_percentage"))) - percentage) < 0.011
                ):
                    matches.append(str(item["code"]))
            except (KeyError, TypeError, ValueError):
                continue
        if len(matches) != 1:
            raise ValueError(f"Unable to uniquely map official industry index to history code: {name} matches={matches}")
        codes[name] = matches[0]
    return [
        {
            "dataset_key": f"twse:{name}",
            "market": "twse",
            "industry": name,
            "source": source,
            "history_code": codes[name],
            "history_source": history_source_template.format(code=codes[name]),
            "timezone": "Asia/Taipei",
            "available_time": config["markets"]["twse"]["available_time"],
            "official_fields": ["close", "change", "change_percent"],
            "nullable_unavailable_fields": ["open", "high", "low", "trading_value"],
            "discovered_on": discovery_date.isoformat(),
        }
        for name in names
    ]


def build_registry(client: OfficialClient, config: dict[str, Any], end: date) -> dict[str, Any]:
    twse = discover_twse_registry(client, config, end) if config["markets"]["twse"]["enabled"] else []
    tpex = discover_tpex_registry(client, config, end) if config["markets"]["tpex"]["enabled"] else []
    payload = {
        "version": "1.0",
        "generated_at": _now_iso(),
        "timezone": "Asia/Taipei",
        "markets": {
            "twse": {"industry_index_count": len(twse), "datasets": twse},
            "tpex": {
                "industry_index_count": len(tpex),
                "datasets": tpex,
                "official_industry_index_available": True,
                "source": config["markets"]["tpex"]["source"],
                "note": config["markets"]["tpex"]["note"],
            },
        },
    }
    _write_json_atomic(REGISTRY_PATH, payload)
    return payload


def discover_tpex_registry(client: OfficialClient, config: dict[str, Any], end: date) -> list[dict[str, Any]]:
    source = config["markets"]["tpex"]["source"]
    month = end.replace(day=1)
    payload = client.post_json(source, {"date": month.isoformat().replace("-", "/")})
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if payload.get("stat") != "ok" or not isinstance(tables, list) or not tables:
        raise ValueError("Unable to discover the TPEx industry-index registry")
    fields = tables[0].get("fields")
    if not isinstance(fields, list) or len(fields) < 2:
        raise ValueError("TPEx industry-index field catalog changed")
    return [
        {
            "dataset_key": f"tpex:{name}",
            "market": "tpex",
            "industry": name,
            "source": source,
            "timezone": "Asia/Taipei",
            "available_time": config["markets"]["tpex"]["available_time"],
            "official_fields": ["close"],
            "derived_fields": ["change", "change_percent"],
            "nullable_unavailable_fields": ["open", "high", "low", "trading_value"],
            "discovered_on": end.isoformat(),
        }
        for raw_name in fields[1:]
        if (name := f"{str(raw_name).strip()}{INDUSTRY_SUFFIX}")
    ]


def upgrade_registry_history_codes(client: OfficialClient, registry: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Add bulk-history codes by matching the latest already verified TWSE values."""

    rows = [row for row in load_existing_rows() if row["market"] == "twse"]
    if not rows:
        raise ValueError("Cannot upgrade the registry without verified TWSE observations")
    latest = max(_as_date(row["trade_date"]) for row in rows)
    latest_rows = {str(row["industry"]): row for row in rows if _as_date(row["trade_date"]) == latest}
    history_indices = client.get_json(config["markets"]["twse"]["history_registry_source"])
    template = config["markets"]["twse"]["history_source_template"]
    for item in registry["markets"]["twse"]["datasets"]:
        observed = latest_rows.get(item["industry"])
        if observed is None:
            raise ValueError(f"Latest TWSE observation missing for registry upgrade: {item['industry']}")
        matches = []
        for candidate in history_indices:
            try:
                if (
                    abs(_number(candidate.get("index")) - float(observed["close"])) < 0.011
                    and abs(abs(_number(candidate.get("volatility_points"))) - abs(float(observed["change"]))) < 0.011
                    and abs(abs(_number(candidate.get("volatility_percentage"))) - abs(float(observed["change_percent"]))) < 0.011
                ):
                    matches.append(str(candidate["code"]))
            except (KeyError, TypeError, ValueError):
                continue
        if len(matches) != 1:
            raise ValueError(f"Unable to upgrade official history code: {item['industry']} matches={matches}")
        item["history_code"] = matches[0]
        item["history_source"] = template.format(code=matches[0])
    registry["generated_at"] = _now_iso()
    registry["history_registry_source"] = config["markets"]["twse"]["history_registry_source"]
    _write_json_atomic(REGISTRY_PATH, registry)
    return registry


def trading_dates(start: date, end: date) -> list[date]:
    """Use the existing local Taiwan stock lake as the actual trading calendar."""

    files = list((PROJECT_ROOT / "data_lake" / "stock_daily_prices").rglob("*.parquet"))
    if files:
        import duckdb

        connection = duckdb.connect()
        try:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM read_parquet(?, hive_partitioning=true) "
                "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                [str(STOCK_PRICE_GLOB).replace("\\", "/"), start, end],
            ).fetchall()
            dates = [row[0] for row in rows]
            if dates:
                # The stock lake can trail the official after-trading endpoint by
                # one completed session. Probe only newer weekdays so forward
                # incremental updates do not wait for another pipeline.
                latest = dates[-1]
                dates.extend(
                    candidate
                    for offset in range(1, (end - latest).days + 1)
                    if (candidate := latest + timedelta(days=offset)).weekday() < 5
                )
                return dates
        finally:
            connection.close()
    LOGGER.warning("Local stock calendar unavailable; falling back to weekdays for request planning")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1) if (start + timedelta(days=offset)).weekday() < 5]


def load_existing_rows() -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    rows: list[dict[str, Any]] = []
    for path in sorted(LAKE_ROOT.glob("market=*/year=*/indices.parquet")):
        rows.extend(pq.ParquetFile(path).read().to_pylist())
    return rows


def plan_missing_dates(
    dates: Iterable[date],
    registry: dict[str, Any],
    options: RunOptions,
    existing_rows: Iterable[dict[str, Any]],
    known_gaps: set[tuple[str, str, date]] | None = None,
) -> list[date]:
    selected = _selected_datasets(registry, options)
    expected = {(item["market"], item["industry"]) for item in selected}
    coverage: dict[date, set[tuple[str, str]]] = {}
    for row in existing_rows:
        trade_date = _as_date(row["trade_date"])
        coverage.setdefault(trade_date, set()).add((str(row["market"]), str(row["industry"])))
    gaps_by_date: dict[date, set[tuple[str, str]]] = {}
    for market, industry, trade_date in known_gaps or set():
        gaps_by_date.setdefault(trade_date, set()).add((market, industry))
    planned = []
    for trade_date in dates:
        satisfied = coverage.get(trade_date, set()) | gaps_by_date.get(trade_date, set())
        if options.refresh or not expected.issubset(satisfied):
            planned.append(trade_date)
    return planned


def fetch_twse_date(client: OfficialClient, config: dict[str, Any], trade_date: date) -> list[dict[str, Any]]:
    source = config["markets"]["twse"]["source"]
    sources = [source, *config["markets"]["twse"].get("official_mirrors", [])]
    payload = client.get_json(sources, {"date": trade_date.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
    if payload.get("stat") != "OK":
        raise ValueError(f"TWSE returned no data for {trade_date}: {payload.get('stat')}")
    table = _price_index_table(payload)
    rows: list[dict[str, Any]] = []
    for raw in table["data"]:
        name = str(raw[0]).strip()
        if not name.endswith(INDUSTRY_SUFFIX):
            continue
        if any(str(raw[index]).replace(",", "").strip() in {"", "--", "---"} for index in (1, 3, 4)):
            # The registry reflects the current official classifications. Newer
            # indices legitimately have no values before their inception.
            continue
        close = _number(raw[1])
        points = abs(_number(raw[3]))
        sign = -1.0 if _sign(raw[2]) == "-" else 1.0
        change = 0.0 if points == 0 else sign * points
        change_percent = 0.0 if _number(raw[4]) == 0 else sign * abs(_number(raw[4]))
        rows.append(
            {
                "market": "twse",
                "industry": name,
                "trade_date": trade_date,
                "open": None,
                "high": None,
                "low": None,
                "close": close,
                "change": change,
                "change_percent": change_percent,
                "trading_value": None,
                "source": source,
                "available_at": f"{trade_date.isoformat()}T18:00:00+08:00",
            }
        )
    if not rows:
        raise ValueError(f"TWSE response contains no industry indices for {trade_date}")
    return rows


def fetch_twse_history(client: OfficialClient, dataset: dict[str, Any], start: date, end: date) -> list[dict[str, Any]]:
    payload = client.get_json(dataset["history_source"], {"start": start.isoformat(), "end": end.isoformat()})
    if payload.get("empty") is True:
        return []
    data = payload.get("data")
    labels = data.get("labels") if isinstance(data, dict) else None
    series = data.get("datasets") if isinstance(data, dict) else None
    if not isinstance(labels, list) or not isinstance(series, list):
        raise ValueError(f"Taiwan Index Plus history format changed: {dataset['industry']}")
    values = {str(item.get("value_type")): item.get("data") for item in series if isinstance(item, dict)}
    required = ("price", "volatility_points", "volatility_percentage")
    if any(not isinstance(values.get(key), list) or len(values[key]) != len(labels) for key in required):
        raise ValueError(f"Taiwan Index Plus history series incomplete: {dataset['industry']}")
    rows = []
    for index, label in enumerate(labels):
        trade_date = datetime.strptime(str(label), "%Y/%m/%d").date()
        if not start <= trade_date <= end:
            raise ValueError("Official history returned a date outside the requested range")
        rows.append(
            {
                "market": "twse",
                "industry": dataset["industry"],
                "trade_date": trade_date,
                "open": None,
                "high": None,
                "low": None,
                "close": _number(values["price"][index]),
                "change": _number(values["volatility_points"][index]),
                "change_percent": _number(values["volatility_percentage"][index]),
                "trading_value": None,
                "source": dataset["history_source"],
                "available_at": f"{trade_date.isoformat()}T18:00:00+08:00",
            }
        )
    return rows


def fetch_tpex_history(
    client: OfficialClient,
    datasets: list[dict[str, Any]],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Fetch TPEx monthly tables once and fan them out to industry rows."""

    if not datasets:
        return []
    source = datasets[0]["source"]
    selected = {item["industry"] for item in datasets}
    # Include the prior month so the first requested day's change is derived
    # from an official previous close rather than invented or left ambiguous.
    cursor = (start.replace(day=1) - timedelta(days=1)).replace(day=1)
    final_month = end.replace(day=1)
    closes: dict[str, list[tuple[date, float]]] = {name: [] for name in selected}
    while cursor <= final_month:
        payload = client.post_json(source, {"date": cursor.isoformat().replace("-", "/")})
        tables = payload.get("tables") if isinstance(payload, dict) else None
        if payload.get("stat") != "ok" or not isinstance(tables, list) or not tables:
            raise ValueError(f"TPEx returned no industry-index table for {cursor:%Y-%m}")
        fields = tables[0].get("fields")
        data = tables[0].get("data")
        if not isinstance(fields, list) or not isinstance(data, list):
            raise ValueError("TPEx industry-index monthly format changed")
        columns = {f"{str(name).strip()}{INDUSTRY_SUFFIX}": index for index, name in enumerate(fields[1:], 1)}
        for raw in data:
            trade_date = _roc_date(str(raw[0]))
            for industry in selected:
                column = columns.get(industry)
                if column is not None and column < len(raw) and str(raw[column]).strip() not in {"", "--", "---"}:
                    closes[industry].append((trade_date, _number(raw[column])))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    rows: list[dict[str, Any]] = []
    for industry, observations in closes.items():
        ordered = sorted(set(observations))
        for index, (trade_date, close) in enumerate(ordered):
            if not start <= trade_date <= end:
                continue
            previous = ordered[index - 1][1] if index else None
            change = close - previous if previous is not None else None
            change_percent = change / previous * 100 if previous not in {None, 0} else None
            rows.append(
                {
                    "market": "tpex",
                    "industry": industry,
                    "trade_date": trade_date,
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": close,
                    "change": change,
                    "change_percent": change_percent,
                    "trading_value": None,
                    "source": source,
                    "available_at": f"{trade_date.isoformat()}T18:00:00+08:00",
                }
            )
    return rows


def merge_rows_atomic(new_rows: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema(
        [
            ("market", pa.string()),
            ("industry", pa.string()),
            ("trade_date", pa.date32()),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("change", pa.float64()),
            ("change_percent", pa.float64()),
            ("trading_value", pa.float64()),
            ("source", pa.string()),
            ("available_at", pa.string()),
        ]
    )
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in new_rows:
        grouped.setdefault((row["market"], _as_date(row["trade_date"]).year), []).append(row)
    for (market, year), updates in grouped.items():
        path = LAKE_ROOT / f"market={market}" / f"year={year}" / "indices.parquet"
        existing = pq.ParquetFile(path).read().to_pylist() if path.exists() else []
        merged = {(str(row["market"]), str(row["industry"]), _as_date(row["trade_date"])): row for row in existing}
        for row in updates:
            normalized = dict(row)
            normalized["trade_date"] = _as_date(normalized["trade_date"])
            merged[(normalized["market"], normalized["industry"], normalized["trade_date"])] = normalized
        ordered = sorted(merged.values(), key=lambda row: (_as_date(row["trade_date"]), str(row["industry"])))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            pq.write_table(pa.Table.from_pylist(ordered, schema=schema), temporary, compression="zstd", compression_level=6, use_dictionary=["market", "industry", "source"])
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def run_backfill(options: RunOptions, config: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    client = OfficialClient()
    cached_registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8")) if REGISTRY_PATH.exists() else None
    cache_has_history_codes = bool(
        cached_registry
        and all(item.get("history_code") for item in cached_registry["markets"]["twse"]["datasets"])
    )
    cache_has_tpex = bool(cached_registry and cached_registry["markets"]["tpex"]["datasets"])
    if cached_registry and cache_has_tpex and not options.refresh:
        registry = cached_registry if cache_has_history_codes else upgrade_registry_history_codes(client, cached_registry, config)
    else:
        registry = build_registry(client, config, options.end)
    selected = _selected_datasets(registry, options)
    if not selected:
        return _finish_manifest(registry, options, client.request_count, 0, {}, time.perf_counter() - started)
    coverage = _load_coverage()
    twse_selected = [item for item in selected if item["market"] == "twse"]
    tpex_selected = [item for item in selected if item["market"] == "tpex"]
    jobs_to_run: list[tuple[dict[str, Any], date, date]] = []
    for dataset in twse_selected:
        key = dataset["dataset_key"]
        ranges = [(options.start, options.end)] if options.refresh else _missing_ranges(options.start, options.end, coverage.get(key, []))
        jobs_to_run.extend((dataset, start, end) for start, end in ranges)
    tpex_ranges: list[tuple[date, date]] = []
    if tpex_selected:
        if options.refresh:
            tpex_ranges = [(options.start, options.end)]
        else:
            # Every monthly response contains all TPEx industries. Fetch the
            # union of missing ranges once, then update each selected dataset.
            missing = []
            for dataset in tpex_selected:
                missing.extend(_missing_ranges(options.start, options.end, coverage.get(dataset["dataset_key"], [])))
            tpex_ranges = _merge_ranges(missing)
    failures: dict[str, str] = {}
    fetched_dates = 0
    checkpoint = {
        "version": "1.0",
        "started_at": _now_iso(),
        "range": {"start": options.start.isoformat(), "end": options.end.isoformat()},
        "planned_ranges": len(jobs_to_run) + len(tpex_ranges),
        "completed_ranges": [],
        "failures": {},
    }
    _write_json_atomic(CHECKPOINT_PATH, checkpoint)
    with ThreadPoolExecutor(max_workers=options.workers) as executor:
        futures = {
            executor.submit(fetch_twse_history, client, dataset, start, end): (dataset, start, end)
            for dataset, start, end in jobs_to_run
        }
        for future in as_completed(futures):
            dataset, range_start, range_end = futures[future]
            range_key = f"{dataset['dataset_key']}:{range_start}:{range_end}"
            try:
                rows = future.result()
                if rows:
                    merge_rows_atomic(rows)
                coverage[dataset["dataset_key"]] = _merge_ranges(
                    [*coverage.get(dataset["dataset_key"], []), (range_start, range_end)]
                )
                _write_coverage(coverage)
                fetched_dates += len(rows)
                checkpoint["completed_ranges"].append(range_key)
            except Exception as exc:  # isolate one official dataset/range failure
                failures[range_key] = str(exc)
        for range_start, range_end in tpex_ranges:
            range_key = f"tpex:all:{range_start}:{range_end}"
            try:
                rows = fetch_tpex_history(client, tpex_selected, range_start, range_end)
                if rows:
                    merge_rows_atomic(rows)
                for dataset in tpex_selected:
                    coverage[dataset["dataset_key"]] = _merge_ranges(
                        [*coverage.get(dataset["dataset_key"], []), (range_start, range_end)]
                    )
                _write_coverage(coverage)
                fetched_dates += len(rows)
                checkpoint["completed_ranges"].append(range_key)
            except Exception as exc:
                failures[range_key] = str(exc)
        checkpoint["failures"] = failures
        checkpoint["updated_at"] = _now_iso()
        _write_json_atomic(CHECKPOINT_PATH, checkpoint)
    return _finish_manifest(
        registry,
        options,
        client.request_count,
        fetched_dates,
        failures,
        time.perf_counter() - started,
    )


def _finish_manifest(
    registry: dict[str, Any],
    options: RunOptions,
    request_count: int,
    fetched_dates: int,
    failures: dict[str, str],
    duration: float,
) -> dict[str, Any]:
    rows = load_existing_rows()
    datasets: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row['market']}:{row['industry']}"
        item = datasets.setdefault(key, {"market": row["market"], "industry": row["industry"], "rows": 0, "first_date": None, "last_date": None})
        value = _as_date(row["trade_date"]).isoformat()
        item["rows"] += 1
        item["first_date"] = value if item["first_date"] is None or value < item["first_date"] else item["first_date"]
        item["last_date"] = value if item["last_date"] is None or value > item["last_date"] else item["last_date"]
    files = list(LAKE_ROOT.glob("market=*/year=*/indices.parquet"))
    manifest = {
        "version": "1.0",
        "updated_at": _now_iso(),
        "timezone": "Asia/Taipei",
        "format": "Parquet",
        "compression": "ZSTD level 6",
        "configured_history_years": load_config()["history_years"],
        "registry_counts": {market: data["industry_index_count"] for market, data in registry["markets"].items()},
        "datasets": dict(sorted(datasets.items())),
        "total_rows": len(rows),
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "last_run": {
            "start": options.start.isoformat(),
            "end": options.end.isoformat(),
            "markets": list(options.markets),
            "industry": options.industry,
            "refresh": options.refresh,
            "official_requests": request_count,
            "fetched_dates": fetched_dates,
            "failures": failures,
            "duration_seconds": round(duration, 6),
        },
    }
    _write_json_atomic(MANIFEST_PATH, manifest)
    return manifest


def _selected_datasets(registry: dict[str, Any], options: RunOptions) -> list[dict[str, Any]]:
    result = []
    normalized_filter = _normalize_industry(options.industry) if options.industry else None
    for market in options.markets:
        for item in registry["markets"][market]["datasets"]:
            if normalized_filter is None or item["industry"] == normalized_filter:
                result.append(item)
    if options.industry and not result:
        raise ValueError(f"Official industry index not found: {options.industry}")
    return result


def _normalize_industry(value: str) -> str:
    cleaned = value.strip()
    return cleaned if cleaned.endswith(INDUSTRY_SUFFIX) else f"{cleaned}{INDUSTRY_SUFFIX}"


def _load_coverage() -> dict[str, list[tuple[date, date]]]:
    if not COVERAGE_PATH.exists():
        return {}
    payload = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    return {
        key: [(date.fromisoformat(item["start"]), date.fromisoformat(item["end"])) for item in ranges]
        for key, ranges in payload.get("datasets", {}).items()
    }


def _write_coverage(coverage: dict[str, list[tuple[date, date]]]) -> None:
    payload = {
        "version": "1.0",
        "updated_at": _now_iso(),
        "datasets": {
            key: [{"start": start.isoformat(), "end": end.isoformat()} for start, end in _merge_ranges(ranges)]
            for key, ranges in sorted(coverage.items())
        },
    }
    _write_json_atomic(COVERAGE_PATH, payload)


def _missing_ranges(start: date, end: date, covered: list[tuple[date, date]]) -> list[tuple[date, date]]:
    cursor = start
    missing: list[tuple[date, date]] = []
    for covered_start, covered_end in _merge_ranges(covered):
        if covered_end < cursor or covered_start > end:
            continue
        if cursor < covered_start:
            missing.append((cursor, min(end, covered_start - timedelta(days=1))))
        cursor = max(cursor, covered_end + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        missing.append((cursor, end))
    return missing


def _merge_ranges(ranges: list[tuple[date, date]]) -> list[tuple[date, date]]:
    merged: list[tuple[date, date]] = []
    for start, end in sorted(ranges):
        if start > end:
            raise ValueError("Coverage range start is later than end")
        if not merged or start > merged[-1][1] + timedelta(days=1):
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _load_known_gaps() -> set[tuple[str, str, date]]:
    if not GAPS_PATH.exists():
        return set()
    payload = json.loads(GAPS_PATH.read_text(encoding="utf-8"))
    return {
        (str(item["market"]), str(item["industry"]), date.fromisoformat(str(item["trade_date"])))
        for item in payload.get("gaps", [])
    }


def _write_known_gaps(gaps: set[tuple[str, str, date]]) -> None:
    payload = {
        "version": "1.0",
        "updated_at": _now_iso(),
        "reason": "official value unavailable before index inception; no synthetic value stored",
        "gaps": [
            {"market": market, "industry": industry, "trade_date": trade_date.isoformat()}
            for market, industry, trade_date in sorted(gaps, key=lambda item: (item[2], item[0], item[1]))
        ],
    }
    _write_json_atomic(GAPS_PATH, payload)


def _price_index_table(payload: dict[str, Any]) -> dict[str, Any]:
    for table in payload.get("tables", []):
        fields = table.get("fields") or []
        if fields[:5] == ["指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)"]:
            return table
    raise ValueError("TWSE price-index table format changed")


def _sign(value: Any) -> str:
    text = re.sub(r"<[^>]+>", "", str(value)).strip()
    if "+" in text:
        return "+"
    if "-" in text or "－" in text:
        return "-"
    return "0"


def _number(value: Any) -> float:
    text = str(value).replace(",", "").strip()
    if text in {"", "--", "---"}:
        raise ValueError(f"Missing official numeric value: {value!r}")
    result = float(text)
    if not math.isfinite(result):
        raise ValueError("Official numeric value is not finite")
    return result


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _roc_date(value: str) -> date:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 7:
        raise ValueError(f"Invalid TPEx ROC date: {value!r}")
    return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))


def _subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, month=2, day=28)


def _now_iso() -> str:
    return datetime.now(TAIPEI).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int)
    parser.add_argument("--start")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--market", action="append", choices=("twse", "tpex"))
    parser.add_argument("--industry")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--batch-cooldown", type=float, default=50.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args = parse_args(argv)
    config = load_config()
    framework = json.loads(FRAMEWORK_CONFIG_PATH.read_text(encoding="utf-8"))["defaults"]
    args.workers = args.workers or int(framework["concurrency"])
    args.batch_size = args.batch_size or int(framework["batch_size"])
    years = args.years if args.years is not None else config["history_years"]
    if years <= 0 or not 1 <= args.workers <= 4 or not 1 <= args.batch_size <= 20 or args.batch_cooldown < 0:
        LOGGER.error("years/workers/batch-size/cooldown settings are invalid")
        return 2
    try:
        end = date.fromisoformat(args.end)
        now = datetime.now(TAIPEI)
        if end >= now.date() and now.time() < datetime.strptime(config["markets"]["twse"]["available_time"], "%H:%M:%S").time():
            end = now.date() - timedelta(days=1)
        start = date.fromisoformat(args.start) if args.start else _subtract_years(end, years)
        if start > end:
            raise ValueError("start must not be later than end")
        options = RunOptions(
            start,
            end,
            tuple(args.market or ("twse", "tpex")),
            args.industry,
            args.refresh,
            args.resume,
            args.workers,
            args.batch_size,
            args.batch_cooldown,
        )
        if args.dry_run:
            if not REGISTRY_PATH.exists():
                raise ValueError("dry-run requires the existing local registry; discovery would require network")
            registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            selected = _selected_datasets(registry, options)
            coverage = _load_coverage()
            ranges = {
                item["dataset_key"]: [
                    {"start": lower.isoformat(), "end": upper.isoformat()}
                    for lower, upper in _missing_ranges(options.start, options.end, coverage.get(item["dataset_key"], []))
                ]
                for item in selected
            }
            print(json.dumps({"dry_run": True, "official_requests": 0, "planned_ranges": ranges}, ensure_ascii=False))
            return 0
        manifest = run_backfill(options, config)
        _write_json_atomic(SUMMARY_PATH, manifest)
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Industry-index backfill failed; existing Parquet was preserved: %s", exc)
        return 1
    run = manifest["last_run"]
    LOGGER.info(
        "Industry-index backfill finished | rows=%d | requests=%d | fetched_dates=%d | failures=%d | seconds=%.2f",
        manifest["total_rows"], run["official_requests"], run["fetched_dates"], len(run["failures"]), run["duration_seconds"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
