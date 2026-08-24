"""Unified batch pipeline for every TWSE/TPEx security searchable by the site.

Market-wide OpenAPI tables are fetched once and split by symbol. Expensive
multi-quarter MOPS and valuation history files remain separate artifacts and
are reused when present; they are never duplicated into every stock cache.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from analyze_stock_data import (
    apply_financial_summary,
    chips_analysis,
    distribution,
    financial_trends,
    make_health,
)
from build_peer_analysis import overlay_multi_period
from stock_analysis_summary import build_analysis_summary
from config import PROJECT_ROOT
from update_stock_data import (
    CHIPS_DAILY,
    OUTPUT_DIR,
    URLS,
    OfficialClient,
    atomic_json,
    build_stock,
    fetch_chips_history,
    index_rows,
    roc_date,
    safe_table,
)


LOGGER = logging.getLogger("all_stocks")
FINANCIAL_DIR = OUTPUT_DIR / "financials"
VALUATION_DIR = OUTPUT_DIR / "history"
INDEX_PATH = OUTPUT_DIR / "index.json"
SNAPSHOT_PATH = OUTPUT_DIR / "industry_snapshot.json"
STATS_PATH = OUTPUT_DIR / "build_stats.json"
RULE_PATH = PROJECT_ROOT / "config" / "stock_health_rules.json"
SUMMARY_RULE_PATH = PROJECT_ROOT / "config" / "stock_analysis_summary_rules.json"
CALENDAR_PATH = PROJECT_ROOT / "data" / "calendar" / "twse_trading_calendar.json"

TABLE_KEYS = {
    "twse_profile": "公司代號", "twse_quote": "Code", "twse_valuation": "Code",
    "twse_revenue": "公司代號", "twse_eps": "公司代號", "twse_profitability": "公司代號",
    "twse_income": "公司代號", "twse_balance": "公司代號",
    "tpex_profile": "SecuritiesCompanyCode", "tpex_quote": "SecuritiesCompanyCode",
    "tpex_valuation": "SecuritiesCompanyCode", "tpex_revenue": "公司代號",
    "tpex_eps": "SecuritiesCompanyCode", "tpex_income": "SecuritiesCompanyCode",
    "tpex_balance": "SecuritiesCompanyCode",
}
DAILY_TABLES = {"twse_profile", "tpex_profile", "twse_quote", "tpex_quote", "twse_valuation", "tpex_valuation"}
MONTHLY_TABLES = {"twse_profile", "tpex_profile", "twse_quote", "tpex_quote", "twse_revenue", "tpex_revenue"}
QUARTERLY_TABLES = set(URLS) - {"twse_valuation", "tpex_valuation"}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the unified all-market stock cache")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--daily", action="store_true")
    modes.add_argument("--monthly", action="store_true")
    modes.add_argument("--quarterly", action="store_true")
    parser.add_argument("--symbol", action="append", help="build one symbol; repeatable")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--delay", type=float, default=.15)
    return parser.parse_args()


def instrument(symbol: str, name: str, is_company: bool) -> str:
    if is_company: return "company"
    upper = name.upper()
    if symbol.startswith("00") or "ETF" in upper: return "ETF"
    if symbol.startswith("020") or "ETN" in upper: return "ETN"
    if len(symbol) > 4 or any(token in name for token in ("購", "售", "權證")): return "warrant"
    return "other"


def latest_market_date() -> str:
    with CHIPS_DAILY.open("r", encoding="utf-8-sig") as handle:
        lines = [line.split(",", 1)[0].strip() for line in handle if line.strip()]
    if len(lines) < 2: raise ValueError("chips trading calendar is empty")
    return lines[-1]


def recent_trading_dates(end: str, count: int = 3) -> list[str]:
    calendar = load_json(CALENDAR_PATH) or {}; years = calendar.get("years", {}); cursor = date.fromisoformat(end); output: list[str] = []
    while len(output) < count:
        spec = years.get(str(cursor.year))
        if not spec: break
        text = cursor.isoformat(); closed = set(spec.get("closed_dates", [])); special = set(spec.get("special_open_dates", []))
        if text in special or (cursor.isoweekday() in spec.get("regular_trading_weekdays", [1, 2, 3, 4, 5]) and text not in closed): output.append(text)
        cursor -= timedelta(days=1)
    return sorted(output) or [end]


def load_json(path: Path) -> dict[str, Any] | None:
    try: return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError): return None


def comparable(payload: dict[str, Any] | None) -> Any:
    if payload is None: return None
    value = copy.deepcopy(payload); value.pop("updated_at", None)
    status = value.get("data", {}).get("build_status")
    if isinstance(status, dict): value["data"]["build_status"] = {"state": status.get("state")}
    return value


def merge_history(old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]], limit: int = 60) -> list[dict[str, Any]]:
    rows = {row.get("trade_date"): row for row in old_rows if row.get("trade_date")}
    rows.update({row.get("trade_date"): row for row in new_rows if row.get("trade_date")})
    return [rows[key] for key in sorted(rows)][-limit:]


def enrich(payload: dict[str, Any], snapshot: list[dict[str, Any]], rules: dict[str, Any], summary_rules: dict[str, Any]) -> str:
    data = payload["data"]; symbol = data["profile"]["symbol"]
    if data["profile"]["instrument_type"] != "company":
        data["historical_valuation"] = {"applicable": False, "reason": "ETF／非一般公司不適用公司歷史估值"}
        data["financial_trends"] = {"applicable": False, "reason": "ETF／非一般公司不適用公司財務分析"}
        data["peer_analysis"] = {"applicable": False, "reason": "ETF／非一般公司不適用公司同業排名"}
    else:
        valuation = load_json(VALUATION_DIR / f"{symbol}_valuation.json")
        observations = valuation.get("data", {}).get("observations", []) if valuation else []
        data["historical_valuation"] = ({field: {"current": data["valuation"][field]["value"], "3y": distribution(observations, field, data["valuation"][field]["value"], 3), "5y": distribution(observations, field, data["valuation"][field]["value"], 5), "source_frequency": "monthly_last_available_trading_day"} for field in ("pe", "pb", "dividend_yield")} if observations else {"applicable": False, "reason": "歷史估值尚未建立"})
        financial = load_json(FINANCIAL_DIR / f"{symbol}.json")
        rows = financial.get("data", {}).get("quarters", []) if financial else []
        data["financial_trends"] = financial_trends(rows)
        apply_financial_summary(data, data["financial_trends"])
        current = next((row for row in snapshot if row.get("symbol") == symbol), None)
        data["peer_analysis"] = ({"snapshot": "./data/stocks/industry_snapshot.json", "rankings": "./data/stocks/peer_rankings.json", "symbol": symbol} if current else {"applicable": False, "reason": "同業 Snapshot 尚未收錄"})
    history = data["chips"].get("history", [])
    data["chips"]["analysis"] = chips_analysis(history) if history else {"available_days": 0}
    data["health_v2"] = make_health(data, rules)
    data["analysis_summary"] = build_analysis_summary(data, summary_rules, snapshot)
    required = [data["quote"].get("close"), data["valuation"]["pe"].get("value") if data["profile"]["instrument_type"] == "company" else 1]
    if data["profile"]["instrument_type"] == "company" and not data["financial_trends"].get("applicable"): return "partial"
    return "complete" if all(value is not None for value in required) else "partial"


def merge_mode(existing: dict[str, Any] | None, fresh: dict[str, Any], mode: str) -> dict[str, Any]:
    if not existing or mode == "all": return fresh
    old = existing.get("data", {}); new = fresh["data"]
    old["profile"] = new["profile"]
    if mode == "daily":
        old["quote"], old["valuation"] = new["quote"], new["valuation"]
        old["chips"] = new["chips"]
    elif mode == "monthly":
        for key in ("revenue", "revenue_yoy", "revenue_mom"):
            old.setdefault("fundamentals", {})[key] = new.get("fundamentals", {}).get(key)
    elif mode == "quarterly":
        old["fundamentals"] = new["fundamentals"]
    existing["updated_at"] = fresh["updated_at"]; existing["data"] = old; existing["sources"] = {**existing.get("sources", {}), **fresh.get("sources", {})}
    return existing


def directory_size() -> tuple[int, int]:
    files = [path for path in OUTPUT_DIR.glob("*.json") if path.stem not in {"index", "industry_snapshot", "peer_rankings", "build_stats"}]
    return len(files), sum(path.stat().st_size for path in files)


def refresh_snapshot(current: Mapping[str, Mapping[str, Any]], tables: Mapping[str, Mapping[str, Mapping[str, Any]]], existing: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """Refresh only fields available in this frequency run, preserving others."""
    by_symbol = {row["symbol"]: dict(row) for row in existing if row.get("symbol")}
    for symbol, item in current.items():
        if item["instrument_type"] != "company": continue
        try:
            data = build_stock(symbol, item["market"], tables, [])["data"]
            row = by_symbol.get(symbol, {"symbol": symbol})
            row.update({"name": item["name"], "market": item["market"], "industry": data["profile"]["industry"] if data["profile"]["industry"] != "資料不足" else row.get("industry", item["industry"]), "instrument_type": "company"})
            if mode in {"daily", "all"}:
                for key in ("pe", "pb", "dividend_yield"): row[key] = data["valuation"][key]["value"]
                row["valuation_date"] = data["valuation"]["pe"]["data_date"]
            if mode in {"monthly", "all"}:
                row["revenue_yoy"] = data["fundamentals"].get("revenue_yoy", {}).get("value")
                row["revenue_period"] = data["fundamentals"].get("revenue_yoy", {}).get("data_date")
            if mode in {"quarterly", "all"}:
                for key in ("eps", "roe", "gross_margin", "operating_margin", "net_margin", "debt_ratio", "current_ratio"):
                    row[key] = data["fundamentals"].get(key, {}).get("value")
                row["financial_period"] = data["fundamentals"].get("report_period")
                row["financial_date"] = data["fundamentals"].get("report_date")
            by_symbol[symbol] = row
        except Exception as exc: LOGGER.warning("snapshot kept prior row for %s: %s", symbol, exc)
    active = [by_symbol[symbol] for symbol, item in current.items() if item["instrument_type"] == "company" and symbol in by_symbol]
    overlay_multi_period(active)
    prior = load_json(SNAPSHOT_PATH)
    if not prior or prior.get("stocks") != active:
        atomic_json(SNAPSHOT_PATH, {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": "3.0", "stocks": active})
    return active


def main() -> int:
    started = time.monotonic(); initial_files, initial_bytes = directory_size(); options = arguments()
    mode = "daily" if options.daily else "monthly" if options.monthly else "quarterly" if options.quarterly else "all"
    requested_tables = DAILY_TABLES if mode == "daily" else MONTHLY_TABLES if mode == "monthly" else QUARTERLY_TABLES if mode == "quarterly" else set(URLS)
    # Profile and quote tables are always required to refresh the active universe.
    requested_tables |= {"twse_profile", "tpex_profile", "twse_quote", "tpex_quote"}
    client = OfficialClient(options.delay); raw: dict[str, list[Mapping[str, Any]]] = {}; source_timings: dict[str, float] = {}
    for key in URLS:
        tick = time.monotonic(); raw[key] = safe_table(client, key) if key in requested_tables else []
        if key in requested_tables: source_timings[key] = round(time.monotonic() - tick, 3)
    tables = {name: index_rows(rows, TABLE_KEYS[name]) for name, rows in raw.items()}
    old_index = load_json(INDEX_PATH) or {}; old_by_symbol = {row["symbol"]: row for row in old_index.get("stocks", [])}
    current: dict[str, dict[str, Any]] = {}
    for market, key, code_key, name_key in (("TWSE", "twse_quote", "Code", "Name"), ("TPEx", "tpex_quote", "SecuritiesCompanyCode", "CompanyName")):
        prefix = "twse" if market == "TWSE" else "tpex"
        for row in raw[key]:
            symbol = str(row.get(code_key, "")).strip(); name = str(row.get(name_key, "")).strip()
            if not symbol: continue
            profile = tables[f"{prefix}_profile"].get(symbol, {}); revenue = tables[f"{prefix}_revenue"].get(symbol, {})
            kind = instrument(symbol, name, bool(profile)); current[symbol] = {"symbol": symbol, "name": name, "market": market, "industry": revenue.get("產業別") if kind == "company" else "ETF／其他", "instrument_type": kind, "active": True}
    universe = list(current.values()) + [{**row, "active": False} for symbol, row in old_by_symbol.items() if symbol not in current]
    # A limited batch prioritizes analyzable companies; the unlimited run still
    # includes ETFs/ETNs/warrants so every active instrument remains searchable.
    selected = sorted(current, key=lambda symbol: (current[symbol]["instrument_type"] != "company", symbol))
    if options.symbol: selected = [symbol for symbol in options.symbol if symbol in current]
    if options.limit is not None: selected = selected[:max(0, options.limit)]
    # Daily/default fetches only the latest batch chips row. Existing history is retained.
    chips = {symbol: [] for symbol in selected}
    if mode in {"daily", "all"} and selected:
        tick = time.monotonic(); market_map = {symbol: current[symbol]["market"] for symbol in selected}
        date_specs = (("TWSE", "twse_quote", "Date"), ("TPEx", "tpex_quote", "Date"))
        for market_name, table_name, date_key in date_specs:
            market_symbols = {symbol for symbol in selected if market_map[symbol] == market_name}
            available_dates = [roc_date(row.get(date_key)) for row in raw[table_name]]
            day = max((value for value in available_dates if value), default=latest_market_date())
            if market_symbols:
                # Latest quote can precede publication of the matching chips
                # tables. Stop at the first official trading date that yields
                # any records instead of repeatedly requesting every symbol.
                for candidate in reversed(recent_trading_dates(day)):
                    result = fetch_chips_history(client, [candidate], market_symbols, market_map)
                    for symbol, rows in result.items(): chips[symbol].extend(rows)
                    if any(result.values()): break
        source_timings["latest_chips_twse_tpex"] = round(time.monotonic() - tick, 3)
    snapshot_payload = load_json(SNAPSHOT_PATH) or {"stocks": []}
    snapshot = refresh_snapshot(current, tables, snapshot_payload.get("stocks", []), mode)
    rules = json.loads(RULE_PATH.read_text(encoding="utf-8"))
    summary_rules = json.loads(SUMMARY_RULE_PATH.read_text(encoding="utf-8")); complete = partial = failed = unchanged = 0
    for symbol in selected:
        path = OUTPUT_DIR / f"{symbol}.json"; existing = load_json(path)
        try:
            old_history = existing.get("data", {}).get("chips", {}).get("history", []) if existing else []
            history = merge_history(old_history, chips.get(symbol, []))
            fresh = build_stock(symbol, current[symbol]["market"], tables, history)
            fresh["data"]["profile"]["instrument_type"] = current[symbol]["instrument_type"]
            payload = merge_mode(existing, fresh, mode); state = enrich(payload, snapshot, rules, summary_rules)
            payload["data"]["build_status"] = {"state": state, "mode": mode, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            if comparable(existing) == comparable(payload): unchanged += 1
            else: atomic_json(path, payload)
            complete += state == "complete"; partial += state == "partial"
            current[symbol]["cached"] = True; current[symbol]["cache_status"] = state
        except Exception as exc:
            failed += 1; LOGGER.error("%s failed; existing cache preserved: %s", symbol, exc)
            current[symbol]["cached"] = path.exists(); current[symbol]["cache_status"] = "failed"
    for row in universe:
        if "cached" not in row:
            path = OUTPUT_DIR / f"{row['symbol']}.json"; row["cached"] = path.exists(); row["cache_status"] = old_by_symbol.get(row["symbol"], {}).get("cache_status", "not_built" if not path.exists() else "unknown")
    atomic_json(INDEX_PATH, {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": "2.0", "active_count": len(current), "company_count": sum(row["instrument_type"] == "company" for row in current.values()), "stocks": sorted(universe, key=lambda row: row["symbol"])})
    file_count, total_bytes = directory_size(); duration = round(time.monotonic() - started, 3)
    stats = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": mode, "selected": len(selected), "universe": {"active": len(current), "companies": sum(row["instrument_type"] == "company" for row in current.values()), "inactive_retained": len(universe) - len(current)}, "results": {"complete": complete, "partial": partial, "failed": failed, "unchanged": unchanged}, "source_timings_seconds": dict(sorted(source_timings.items(), key=lambda item: item[1], reverse=True)), "storage": {"stock_json_files": file_count, "total_bytes": total_bytes, "average_bytes": round(total_bytes / file_count, 2) if file_count else 0, "build_increment_bytes": total_bytes - initial_bytes, "build_increment_files": file_count - initial_files, "estimated_annual_full_rewrite_upper_bound_bytes": total_bytes * 250, "note": "Git delta compression normally uses less than the full-rewrite upper bound"}, "duration_seconds": duration}
    atomic_json(STATS_PATH, stats); LOGGER.info("Summary | mode=%s selected=%d complete=%d partial=%d failed=%d duration=%.2fs", mode, len(selected), complete, partial, failed, duration)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    raise SystemExit(main())
