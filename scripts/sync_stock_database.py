"""Import the existing static stock JSON cache into PostgreSQL.

The JSON build remains authoritative.  This command performs no network
requests and can be safely rerun because every destination table is UPSERTed.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from database.connection import apply_migrations, connect
from database.repository import StockRepository


LOGGER = logging.getLogger("stock_database_sync")
STOCKS = PROJECT_ROOT / "data" / "stocks"
INDEX = STOCKS / "index.json"
FINANCIALS = STOCKS / "financials"
VALUATIONS = STOCKS / "history"
PEER_RANKINGS = STOCKS / "peer_rankings.json"
TABLES = (
    "stocks", "stock_quotes", "stock_valuations", "stock_monthly_revenue",
    "stock_financials", "stock_chips", "stock_health", "industry_rankings",
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dual-write stock JSON caches to PostgreSQL")
    parser.add_argument("--init-schema", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--full", action="store_true")
    modes.add_argument("--daily", action="store_true")
    modes.add_argument("--monthly", action="store_true")
    modes.add_argument("--quarterly", action="store_true")
    parser.add_argument("--symbol", action="append", help="limit to a symbol; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="parse/count JSON without a DB connection")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("Skipped invalid JSON %s: %s", path, exc)
        return None


def iso_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        text += "-01"
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def numeric(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def metric(block: dict[str, Any], key: str) -> Any:
    item = block.get(key) or {}
    return item.get("value") if isinstance(item, dict) else item


def metric_date(block: dict[str, Any], key: str) -> str | None:
    item = block.get(key) or {}
    return iso_date(item.get("data_date")) if isinstance(item, dict) else None


def valuation_row(symbol: str, item: dict[str, Any]) -> dict[str, Any] | None:
    pe_date, pb_date, yield_date = (
        iso_date(item.get("pe_date") or (item.get("pe") or {}).get("data_date") if isinstance(item.get("pe"), dict) else item.get("trade_date")),
        iso_date(item.get("pb_date") or (item.get("pb") or {}).get("data_date") if isinstance(item.get("pb"), dict) else item.get("trade_date")),
        iso_date(item.get("dividend_yield_date") or (item.get("dividend_yield") or {}).get("data_date") if isinstance(item.get("dividend_yield"), dict) else item.get("trade_date")),
    )
    valuation_date = max(filter(None, (iso_date(item.get("trade_date")), pe_date, pb_date, yield_date)), default=None)
    if not valuation_date:
        return None
    def val(name: str):
        value = item.get(name)
        return numeric(value.get("value")) if isinstance(value, dict) else numeric(value)
    return {
        "symbol": symbol, "valuation_date": valuation_date,
        "pe": val("pe"), "pb": val("pb"), "dividend_yield": val("dividend_yield"),
        "pe_date": pe_date or valuation_date, "pb_date": pb_date or valuation_date,
        "dividend_yield_date": yield_date or valuation_date, "source_payload": item,
    }


def build_batches(mode: str, symbols: set[str] | None = None) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    index = read_json(INDEX)
    if not index:
        raise RuntimeError(f"Stock universe is unavailable: {INDEX}")
    universe = [row for row in index.get("stocks", []) if not symbols or row.get("symbol") in symbols]
    batches = {table: [] for table in TABLES}
    source_updated_at = index.get("updated_at")
    for item in universe:
        symbol = str(item["symbol"])
        batches["stocks"].append({
            "symbol": symbol, "name": item.get("name") or symbol,
            "market": item.get("market") or "unknown", "industry": item.get("industry"),
            "instrument_type": item.get("instrument_type") or "other",
            "active": bool(item.get("active", True)), "cached": item.get("cached"),
            "cache_status": item.get("cache_status"), "source_updated_at": source_updated_at,
        })
        payload = read_json(STOCKS / f"{symbol}.json")
        if not payload:
            continue
        data = payload.get("data", {})
        quote = data.get("quote") or {}
        trade_date = iso_date(quote.get("trade_date"))
        if mode in {"full", "daily"} and trade_date:
            units = quote.get("unit") or {}
            batches["stock_quotes"].append({
                "symbol": symbol, "trade_date": trade_date, "close": numeric(quote.get("close")),
                "change": numeric(quote.get("change")), "change_percent": numeric(quote.get("change_percent")),
                "volume": numeric(quote.get("volume")), "price_unit": units.get("price"),
                "volume_unit": units.get("volume"), "source_payload": quote,
            })
        if mode in {"full", "daily"}:
            current = valuation_row(symbol, data.get("valuation") or {})
            valuations: dict[str, dict[str, Any]] = {}
            if mode == "full":
                history = read_json(VALUATIONS / f"{symbol}_valuation.json")
                for observation in (history or {}).get("data", {}).get("observations", []):
                    row = valuation_row(symbol, observation)
                    if row:
                        valuations[row["valuation_date"]] = row
            if current:
                valuations[current["valuation_date"]] = current
            batches["stock_valuations"].extend(valuations.values())
        fundamentals = data.get("fundamentals") or {}
        if mode in {"full", "monthly"}:
            month = metric_date(fundamentals, "revenue_yoy") or metric_date(fundamentals, "revenue_mom") or metric_date(fundamentals, "revenue")
            if month:
                revenue_item = fundamentals.get("revenue") or {}
                revenue_date = metric_date(fundamentals, "revenue")
                # The current cache may combine a quarterly revenue amount with
                # monthly YoY/MoM rates. Never label that quarterly amount as a
                # monthly observation; preserve it only inside source_payload.
                same_month_revenue = bool(revenue_date and revenue_date[:7] == month[:7])
                batches["stock_monthly_revenue"].append({
                    "symbol": symbol, "revenue_month": month,
                    "revenue": numeric(metric(fundamentals, "revenue")) if same_month_revenue else None,
                    "revenue_yoy": numeric(metric(fundamentals, "revenue_yoy")),
                    "revenue_mom": numeric(metric(fundamentals, "revenue_mom")),
                    "unit": revenue_item.get("unit") if same_month_revenue and isinstance(revenue_item, dict) else None,
                    "source_payload": {key: fundamentals.get(key) for key in ("revenue", "revenue_yoy", "revenue_mom")},
                })
        if mode in {"full", "daily"}:
            chips = data.get("chips") or {}; rows = chips.get("history") or []
            if mode == "daily" and rows:
                rows = rows[-1:]
            units = chips.get("unit") or {}
            for row in rows:
                if not iso_date(row.get("trade_date")):
                    continue
                batches["stock_chips"].append({
                    "symbol": symbol, "trade_date": iso_date(row["trade_date"]),
                    "foreign_net": numeric(row.get("foreign_net")),
                    "investment_trust_net": numeric(row.get("investment_trust_net")),
                    "dealer_net": numeric(row.get("dealer_net")),
                    "institutional_total": numeric(row.get("institutional_total")),
                    "margin_balance": numeric(row.get("margin_balance")),
                    "margin_change": numeric(row.get("margin_change")),
                    "short_balance": numeric(row.get("short_balance")),
                    "short_change": numeric(row.get("short_change")),
                    "institutional_unit": units.get("institutional"), "margin_unit": units.get("margin"),
                    "source_payload": row,
                })
        if mode in {"full", "quarterly"}:
            financial = read_json(FINANCIALS / f"{symbol}.json")
            fin_data = (financial or {}).get("data", {})
            scope = fin_data.get("statement_scope") or ""
            for row in fin_data.get("quarters", []):
                period_end, available = iso_date(row.get("period_end")), iso_date(row.get("available_date"))
                if not period_end or not available or not row.get("fiscal_year") or not row.get("quarter"):
                    continue
                unit = row.get("unit") or {}
                batches["stock_financials"].append({
                    "symbol": symbol, "fiscal_year": int(row["fiscal_year"]), "quarter": int(row["quarter"]),
                    "statement_scope": scope, "period_end": period_end,
                    "published_date": iso_date(row.get("published_date")), "available_date": available,
                    "eps": numeric(row.get("eps")), "revenue": numeric(row.get("revenue")),
                    "gross_margin": numeric(row.get("gross_margin")), "operating_margin": numeric(row.get("operating_margin")),
                    "net_margin": numeric(row.get("net_margin")), "roe": numeric(row.get("roe")),
                    "total_assets": numeric(row.get("total_assets")), "total_liabilities": numeric(row.get("total_liabilities")),
                    "debt_ratio": numeric(row.get("debt_ratio")), "current_ratio": numeric(row.get("current_ratio")),
                    "operating_cash_flow": numeric(row.get("operating_cash_flow")),
                    "investing_cash_flow": numeric(row.get("investing_cash_flow")),
                    "capital_expenditure": numeric(row.get("capital_expenditure")), "free_cash_flow": numeric(row.get("free_cash_flow")),
                    "monetary_unit": unit.get("monetary"), "source": row.get("source"), "source_payload": row,
                })
            health = data.get("health_v2") or {}
            rules_version = str(health.get("rules_version") or "")
            fallback_date = iso_date(fundamentals.get("report_date")) or trade_date or "1970-01-01"
            for category, items in (health.get("categories") or {}).items():
                for health_item in items or []:
                    value = health_item.get("value")
                    batches["stock_health"].append({
                        "symbol": symbol, "as_of_date": iso_date(health_item.get("data_date")) or fallback_date,
                        "rules_version": rules_version, "category": category,
                        "metric_key": health_item.get("key") or health_item.get("label") or "unknown",
                        "source_date": iso_date(health_item.get("data_date")),
                        "value_numeric": numeric(value), "value_text": value if isinstance(value, str) else None,
                        "threshold_text": health_item.get("threshold"), "status": health_item.get("status"),
                        "unit": health_item.get("unit"), "note": health_item.get("note"),
                        "source_payload": health_item,
                    })
    if mode in {"full", "quarterly"}:
        peer = read_json(PEER_RANKINGS) or {}
        for industry, group in peer.get("industries", {}).items():
            for key, ranking in (group.get("rankings") or {}).items():
                candidates = list(ranking.get("top10") or [])
                if ranking.get("current_company"):
                    candidates.append(ranking["current_company"])
                seen: set[str] = set()
                for row in candidates:
                    symbol = str(row.get("symbol") or "")
                    if not symbol or symbol in seen or (symbols and symbol not in symbols):
                        continue
                    seen.add(symbol)
                    batches["industry_rankings"].append({
                        "symbol": symbol, "industry": industry, "metric_key": key,
                        "comparison_period": str(ranking.get("comparison_period") or ""),
                        "company_value": numeric(row.get("value")), "industry_median": numeric(ranking.get("median")),
                        "percentile": numeric(row.get("percentile")), "rank": row.get("rank"),
                        "sample_size": ranking.get("sample_size"), "relative_status": row.get("relative_status"),
                        "source_payload": row,
                    })
    return batches, source_updated_at


def materialize_stock_ids(batches: dict[str, list[dict[str, Any]]], ids: dict[str, int]) -> None:
    for table, rows in batches.items():
        if table == "stocks":
            continue
        kept = []
        for row in rows:
            symbol = row.pop("symbol")
            if symbol in ids:
                row["stock_id"] = ids[symbol]
                kept.append(row)
        batches[table] = kept


def sync(connection, batches: dict[str, list[dict[str, Any]]], mode: str, source_updated_at: str | None) -> dict[str, int]:
    repository = StockRepository(connection)
    run_id = repository.start_run(mode, source_updated_at)
    counts: dict[str, int] = {}
    try:
        counts["stocks"] = repository.upsert_many("stocks", batches["stocks"])
        ids = repository.stock_ids(row["symbol"] for row in batches["stocks"])
        materialize_stock_ids(batches, ids)
        for table in TABLES[1:]:
            counts[table] = repository.upsert_many(table, batches[table])
        repository.finish_run(run_id, "success", counts)
        return counts
    except Exception as exc:
        repository.finish_run(run_id, "failed", counts, str(exc))
        raise


def selected_mode(options: argparse.Namespace) -> str | None:
    if options.full: return "full"
    if options.daily: return "daily"
    if options.monthly: return "monthly"
    if options.quarterly: return "quarterly"
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    options = arguments(); mode = selected_mode(options)
    if not mode and not options.init_schema:
        LOGGER.error("Choose --full, --daily, --monthly, --quarterly, or --init-schema")
        return 2
    started = time.monotonic()
    batches: dict[str, list[dict[str, Any]]] | None = None; updated_at = None
    if mode:
        batches, updated_at = build_batches(mode, set(options.symbol or []) or None)
        LOGGER.info("JSON mapping complete | %s", " | ".join(f"{k}={len(v)}" for k, v in batches.items()))
    if options.dry_run:
        LOGGER.info("Dry run complete; PostgreSQL was not contacted | duration=%.2fs", time.monotonic() - started)
        return 0
    connection = connect()
    try:
        with connection.transaction():
            if options.init_schema:
                applied = apply_migrations(connection)
                LOGGER.info("Schema migrations applied: %s", ", ".join(applied) or "none")
            if batches is not None and mode:
                counts = sync(connection, batches, mode, updated_at)
                LOGGER.info("PostgreSQL sync complete | %s", " | ".join(f"{k}={v}" for k, v in counts.items()))
    finally:
        connection.close()
    LOGGER.info("Duration: %.2fs", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOGGER.error("Database sync failed; JSON files were not changed: %s", exc)
        raise SystemExit(1)
