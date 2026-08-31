"""Build an incremental, leakage-safe Taiwan point-in-time foundation."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

from scripts.config import PROJECT_ROOT
from scripts.collectors import AllSourcesUnavailable, CollectorPolicy, OfficialHttpClient, StructuredLog


LOG = logging.getLogger("taiwan_pit")
TAIPEI = ZoneInfo("Asia/Taipei")
CONFIG = PROJECT_ROOT / "config" / "taiwan_pit_registry.json"
ROOT = PROJECT_ROOT / "data_lake" / "taiwan_pit"
COVERAGE = ROOT / "coverage.json"
CHECKPOINT = ROOT / "checkpoint.json"
MANIFEST = ROOT / "manifest.json"
SUMMARY = ROOT / "summary.json"
STRUCTURED_LOG = ROOT / "logs" / "collector.jsonl"
FRAMEWORK_CONFIG = PROJECT_ROOT / "config" / "collector_framework.json"
PRICE_GLOB = str(PROJECT_ROOT / "data_lake" / "stock_daily_prices" / "**" / "*.parquet").replace("\\", "/")
DIM_GLOB = str(PROJECT_ROOT / "data_lake" / "dimensions" / "stocks" / "**" / "*.parquet").replace("\\", "/")
UA = "taiwan-stock-analysis-platform/3.15 PIT-foundation"


class CompatibleTLSAdapter(HTTPAdapter):
    """Keep certificate verification while relaxing OpenSSL's optional SKI check."""

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **kwargs: Any) -> None:
        context = ssl.create_default_context()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = context
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, **kwargs)


class Client(OfficialHttpClient):
    """PIT source adapter over the shared collector transport."""

    def __init__(self) -> None:
        framework = load_json(FRAMEWORK_CONFIG, {})
        super().__init__(CollectorPolicy.from_config(framework.get("defaults")), UA, StructuredLog(STRUCTURED_LOG))

    @property
    def requests(self) -> int:
        return self.request_count

    def session(self) -> requests.Session:
        value = super().session()
        if not getattr(value, "_tpex_tls_compatible", False):
            value.mount("https://www.tpex.org.tw", CompatibleTLSAdapter())
            value._tpex_tls_compatible = True
        return value

    def json(self, method: str, url: str | list[str], **kwargs: Any) -> Any:
        return self.request_json(method, url, **kwargs)


def load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def source_candidates(config: dict[str, Any], key: str) -> list[str]:
    return [config["sources"][key], *config.get("source_fallbacks", {}).get(key, [])]


def primary_source(source: str | list[str]) -> str:
    return source if isinstance(source, str) else source[0]


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


def number(value: Any) -> float | None:
    text = re.sub(r"<[^>]+>", "", str(value)).replace(",", "").strip()
    if text in {"", "--", "---", "N/A"}: return None
    result = float(text)
    return result if math.isfinite(result) else None


def integer(value: Any) -> int | None:
    parsed = number(value)
    return int(parsed) if parsed is not None else None


def roc_date(value: Any) -> date:
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 7: return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:]))
    if len(digits) == 8: return date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:]}")
    raise ValueError(f"invalid official date {value!r}")


def months(start: date, end: date) -> list[date]:
    cursor = start.replace(day=1); output = []
    while cursor <= end:
        output.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return output


def trading_dates(start: date, end: date) -> list[date]:
    import duckdb
    connection = duckdb.connect()
    try:
        return [row[0] for row in connection.execute(
            "SELECT DISTINCT trade_date FROM read_parquet(?, hive_partitioning=true) "
            "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date", [PRICE_GLOB, start, end]
        ).fetchall()]
    finally: connection.close()


def universe() -> dict[str, set[str]]:
    import duckdb
    connection = duckdb.connect()
    try:
        rows = connection.execute(
            "SELECT upper(market), symbol FROM read_parquet(?, hive_partitioning=true) "
            "WHERE instrument_type='company'", [DIM_GLOB]
        ).fetchall()
        result = {"TWSE": set(), "TPEX": set()}
        for market, symbol in rows: result[market].add(str(symbol))
        return result
    finally: connection.close()


def fetch_twse_shares(client: Client, url: str | list[str], day: date, allowed: set[str]) -> list[dict[str, Any]]:
    payload = client.json("GET", url, params={"date": day.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"})
    if payload.get("stat") != "OK": raise ValueError(f"TWSE shares unavailable {day}: {payload.get('stat')}")
    rows = []
    for raw in payload.get("data", []):
        symbol = str(raw[0]).strip()
        if symbol not in allowed: continue
        shares = integer(raw[3])
        rows.append(base_share_row("TWSE", symbol, day, shares, None, primary_source(url)))
    if not rows: raise ValueError(f"TWSE shares returned no company rows {day}")
    return rows


def fetch_tpex_shares(client: Client, url: str | list[str], day: date, allowed: set[str]) -> list[dict[str, Any]]:
    payload = client.json("POST", url, data={"date": day.isoformat().replace("-", "/")})
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not tables: raise ValueError(f"TPEx shares unavailable {day}")
    rows = []
    for raw in tables[0].get("data", []):
        symbol = str(raw[1]).strip()
        if symbol not in allowed: continue
        shares = integer(raw[3]); official_market_value = integer(raw[5])
        rows.append(base_share_row("TPEX", symbol, day, shares, official_market_value, primary_source(url)))
    if not rows: raise ValueError(f"TPEx shares returned no company rows {day}")
    return rows


def base_share_row(market: str, symbol: str, day: date, shares: int | None, official_mv: int | None, source: str) -> dict[str, Any]:
    return {"market": market, "symbol": symbol, "trade_date": day, "issued_shares": shares,
            "official_market_value_million_twd": official_mv, "timezone": "Asia/Taipei", "utc_offset": "+08:00",
            "available_at": f"{day.isoformat()}T20:00:00+08:00", "source": source}


def fetch_twse_actions(client: Client, url: str | list[str], month: date, allowed: set[str]) -> list[dict[str, Any]]:
    end = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    payload = client.json("GET", url, params={"startDate": month.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d"), "response": "json"})
    if payload.get("stat") != "OK": raise ValueError(f"TWSE corporate actions unavailable {month:%Y-%m}")
    return [action_row("TWSE", raw, primary_source(url)) for raw in payload.get("data", []) if str(raw[1]).strip() in allowed]


def fetch_tpex_actions(client: Client, url: str | list[str], month: date, allowed: set[str]) -> list[dict[str, Any]]:
    end = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    payload = client.json("POST", url, data={"startDate": month.isoformat().replace("-", "/"), "endDate": end.isoformat().replace("-", "/")})
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not tables: raise ValueError(f"TPEx corporate actions unavailable {month:%Y-%m}")
    return [action_row("TPEX", raw, primary_source(url)) for raw in tables[0].get("data", []) if str(raw[1]).strip() in allowed]


def action_row(market: str, raw: list[Any], source: str) -> dict[str, Any]:
    day = roc_date(raw[0]); tpex = market == "TPEX"
    return {"market": market, "symbol": str(raw[1]).strip(), "ex_date": day,
            "previous_close": number(raw[3]), "reference_price": number(raw[4]),
            "rights_value": number(raw[5]), "dividend_value": number(raw[6]) if tpex else None,
            "cash_dividend": number(raw[13]) if tpex and len(raw) > 13 else None,
            "stock_dividend_ratio": number(raw[14]) if tpex and len(raw) > 14 else None,
            "action_type": re.sub(r"<[^>]+>", "", str(raw[8] if tpex else raw[6])).strip(),
            "timezone": "Asia/Taipei", "utc_offset": "+08:00",
            # Calculation-result data is conservatively unavailable to models
            # until after the effective session; this cannot create leakage.
            "available_at": f"{day.isoformat()}T20:00:00+08:00", "source": source}


def write_partitioned(rows: list[dict[str, Any]], dataset: str, date_field: str, keys: tuple[str, ...]) -> None:
    if not rows: return
    import pyarrow as pa, pyarrow.parquet as pq
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        day = row[date_field]; groups.setdefault((row.get("market", "TWSE"), day.year), []).append(row)
    for (market, year), additions in groups.items():
        path = ROOT / dataset / f"market={market.lower()}" / f"year={year}" / "part-00000.parquet"
        existing = pq.ParquetFile(path).read().to_pylist() if path.exists() else []
        merged = {tuple(str(row[k]) for k in keys): row for row in existing}
        for row in additions: merged[tuple(str(row[k]) for k in keys)] = row
        ordered = sorted(merged.values(), key=lambda row: tuple(str(row[k]) for k in keys))
        path.parent.mkdir(parents=True, exist_ok=True); temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            pq.write_table(pa.Table.from_pylist(ordered), temp, compression="zstd", compression_level=6)
            os.replace(temp, path)
        finally:
            if temp.exists(): temp.unlink()


def build_sessions(dates: list[date], start: date, end: date) -> list[dict[str, Any]]:
    sessions = set(dates); rows = []
    cursor = start
    while cursor <= end:
        is_open = cursor in sessions
        rows.append({"market": "TWSE_TPEX", "calendar_date": cursor, "is_trading_day": is_open,
                     "session": "regular" if is_open else "closed", "timezone": "Asia/Taipei", "utc_offset": "+08:00",
                     "open_at": f"{cursor.isoformat()}T09:00:00+08:00" if is_open else None,
                     "close_at": f"{cursor.isoformat()}T13:30:00+08:00" if is_open else None,
                     "available_at": f"{cursor.isoformat()}T20:00:00+08:00",
                     "source": "TWSE/TPEx official completed daily-price sessions"})
        cursor += timedelta(days=1)
    return rows


def build_market_cap() -> int:
    import duckdb, pyarrow.parquet as pq
    shares = str(ROOT / "issued_shares" / "**" / "*.parquet").replace("\\", "/")
    if not list((ROOT / "issued_shares").rglob("*.parquet")): return 0
    connection = duckdb.connect()
    try:
        table = connection.execute("""
            SELECT upper(s.market) market,s.symbol,s.trade_date,s.issued_shares,p.close,
                   CASE WHEN s.issued_shares IS NULL OR p.close IS NULL THEN NULL
                        ELSE s.issued_shares*p.close END AS market_cap_twd,
                   s.official_market_value_million_twd,
                   'TWD' currency,'Asia/Taipei' timezone,'+08:00' utc_offset,
                   CAST(greatest(CAST(s.available_at AS TIMESTAMPTZ),CAST(p.available_at AS TIMESTAMPTZ)) AS VARCHAR) available_at,
                   s.source || ' + official close' AS "source"
            FROM read_parquet(?,hive_partitioning=true) s
            LEFT JOIN read_parquet(?,hive_partitioning=true) p
              ON upper(p.market)=upper(s.market) AND p.symbol=s.symbol AND p.trade_date=s.trade_date
        """, [shares, PRICE_GLOB]).to_arrow_table()
        rows = table.to_pylist(); write_partitioned(rows, "daily_market_cap", "trade_date", ("market","symbol","trade_date"))
        return len(rows)
    finally: connection.close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_json(CONFIG, {}); years = args.years or int(config["history_years"])
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end.replace(year=end.year-years)
    dates = trading_dates(start, end); month_list = months(start, end)
    coverage = load_json(COVERAGE, {"dates": {}, "months": {}, "official_gaps": {}})
    coverage.setdefault("dates", {})
    coverage.setdefault("months", {})
    coverage.setdefault("official_gaps", {})
    selected_markets=tuple(args.market or ("TWSE","TPEX"))
    date_jobs = [(market, day) for market in selected_markets for day in dates
                 if args.refresh or (
                     day.isoformat() not in coverage["dates"].get(market, [])
                     and day.isoformat() not in coverage["official_gaps"].get(market, [])
                 )]
    month_jobs = [(market, month) for market in selected_markets for month in month_list
                  if args.refresh or month.strftime("%Y-%m") not in coverage["months"].get(market, [])]
    if args.dataset == "shares": month_jobs=[]
    elif args.dataset == "actions": date_jobs=[]
    plan = {"start":start.isoformat(),"end":end.isoformat(),"trading_days":len(dates),
            "share_requests":len(date_jobs),"corporate_action_requests":len(month_jobs),"network_requests":len(date_jobs)+len(month_jobs)}
    if args.dry_run: return {"dry_run":True,"plan":plan,"official_requests":0}
    started=time.perf_counter(); client=Client(); allowed=universe(); failures={}; share_count=0; action_count=0
    stopped: dict[str, Any] = {}
    atomic_json(CHECKPOINT,{"status":"running","plan":plan,"started_at":datetime.now(TAIPEI).isoformat()})
    def process_batch(jobs: list[tuple[str,date]], kind: str) -> int:
        collected=[]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures={}
            for market,value in jobs:
                if kind=="date":
                    fn=fetch_twse_shares if market=="TWSE" else fetch_tpex_shares
                    key="twse_issued_shares" if market=="TWSE" else "tpex_issued_shares"
                    source=source_candidates(config,key)
                else:
                    fn=fetch_twse_actions if market=="TWSE" else fetch_tpex_actions
                    key="twse_corporate_actions" if market=="TWSE" else "tpex_corporate_actions"
                    source=source_candidates(config,key)
                futures[pool.submit(fn,client,source,value,allowed[market])] = (market,value)
            for future in as_completed(futures):
                market,value=futures[future]
                try:
                    rows=future.result()
                    if kind == "month":
                        rows = [row for row in rows if start <= row["ex_date"] <= end]
                    collected.extend(rows)
                    bucket=coverage["dates" if kind=="date" else "months"].setdefault(market,[])
                    bucket.append(value.isoformat() if kind=="date" else value.strftime("%Y-%m"))
                except AllSourcesUnavailable as exc:
                    message = str(exc)
                    failures[f"{kind}:{market}:{value}"] = message
                    stopped.update({
                        "stopped_reason": "all_official_sources_unavailable",
                        "failed_source": exc.sources,
                        "failed_job": f"{kind}:{market}:{value}",
                    })
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    break
                except Exception as exc:
                    message = str(exc)
                    failures[f"{kind}:{market}:{value}"] = message
                    if kind == "date" and "returned no company rows" in message:
                        coverage["official_gaps"].setdefault(market, []).append(value.isoformat())
        write_partitioned(collected,"issued_shares" if kind=="date" else "corporate_actions",
                          "trade_date" if kind=="date" else "ex_date",
                          ("market","symbol","trade_date") if kind=="date" else ("market","symbol","ex_date","action_type"))
        for section in coverage.values():
            for market in section: section[market]=sorted(set(section[market]))
        atomic_json(COVERAGE,coverage)
        atomic_json(CHECKPOINT,{"status":"running","plan":plan,"official_requests":client.requests,
                               "completed_share_dates":sum(len(v) for v in coverage["dates"].values()),
                               "completed_action_months":sum(len(v) for v in coverage["months"].values()),
                               "failures":failures,"updated_at":datetime.now(TAIPEI).isoformat()})
        return len(collected)

    for offset in range(0,len(date_jobs),args.batch_size):
        share_count += process_batch(date_jobs[offset:offset+args.batch_size],"date")
        if stopped: break
    if not stopped:
        for offset in range(0,len(month_jobs),args.batch_size):
            action_count += process_batch(month_jobs[offset:offset+args.batch_size],"month")
            if stopped: break
    write_partitioned(build_sessions(dates,start,end),"market_sessions","calendar_date",("market","calendar_date"))
    market_cap_rows=build_market_cap()
    remaining_dates=[(market,day.isoformat()) for market,day in date_jobs if day.isoformat() not in coverage["dates"].get(market,[]) and day.isoformat() not in coverage["official_gaps"].get(market,[])]
    remaining_months=[(market,month.strftime("%Y-%m")) for market,month in month_jobs if month.strftime("%Y-%m") not in coverage["months"].get(market,[])]
    remaining_gap={"share_requests":len(remaining_dates),"corporate_action_requests":len(remaining_months),
                   "first_share_gap":remaining_dates[0] if remaining_dates else None,"first_action_gap":remaining_months[0] if remaining_months else None}
    files=list(ROOT.rglob("*.parquet")); result={"version":"1.0","status":"partial" if stopped else "completed","updated_at":datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "range":{"start":start.isoformat(),"end":end.isoformat()},"rows":{"sessions":(end-start).days+1,"issued_shares_fetched":share_count,"corporate_actions_fetched":action_count,"daily_market_cap":market_cap_rows},
        "official_requests":client.requests,"failures":failures,"remaining_gap":remaining_gap,
        "stopped_reason":stopped.get("stopped_reason"),"failed_source":stopped.get("failed_source"),
        "files":len(files),"bytes":sum(p.stat().st_size for p in files),"duration_seconds":round(time.perf_counter()-started,3)}
    atomic_json(MANIFEST,result); atomic_json(SUMMARY,result); atomic_json(CHECKPOINT,result); return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--years",type=int); parser.add_argument("--start");parser.add_argument("--end")
    parser.add_argument("--resume",action="store_true");parser.add_argument("--refresh",action="store_true");parser.add_argument("--dry-run",action="store_true")
    parser.add_argument("--market",action="append",choices=("TWSE","TPEX"));parser.add_argument("--dataset",choices=("shares","actions","all"),default="all")
    parser.add_argument("--workers",type=int,choices=range(1,7)); parser.add_argument("--batch-size",type=int); return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,format="%(levelname)s | %(message)s")
    try:
        args=parse_args(argv); defaults=load_json(FRAMEWORK_CONFIG,{})["defaults"]
        args.workers=args.workers or int(defaults["concurrency"])
        args.batch_size=args.batch_size or int(defaults["batch_size"])
        result=run(args)
    except Exception as exc: LOG.error("PIT backfill failed; existing Parquet preserved: %s",exc); return 1
    print(json.dumps(result,ensure_ascii=False)); return 0


if __name__ == "__main__": raise SystemExit(main())
