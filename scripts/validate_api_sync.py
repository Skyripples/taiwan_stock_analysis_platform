"""Compare REST API responses with PostgreSQL rows and static JSON caches."""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from config import PROJECT_ROOT
from database.connection import connect


REPORT = PROJECT_ROOT / "data" / "analysis" / "api_sync_report.json"
STOCKS = PROJECT_ROOT / "data" / "stocks"
FIXED = ["2330", "2317", "6488", "2881", "1101", "0050"]


def plain(value: Any) -> Any:
    if isinstance(value, Decimal): return int(value) if value == value.to_integral() else float(value)
    if isinstance(value, (date, datetime)): return value.isoformat()
    return value


def atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def compare(failures: list[dict[str, Any]], symbol: str, section: str, field: str, *values: Any) -> None:
    normalized = [plain(item) for item in values]
    if any(item != normalized[0] for item in normalized[1:]):
        failures.append({"symbol": symbol, "section": section, "field": field, "values": normalized})


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--base-url", default="http://127.0.0.1/api/v1")
    options = parser.parse_args(); index = json.loads((STOCKS / "index.json").read_text(encoding="utf-8"))
    universe = sorted(row["symbol"] for row in index["stocks"] if row["symbol"] not in FIXED)
    symbols = FIXED + random.Random(311).sample(universe, 50)
    failures: list[dict[str, Any]] = []; checks = 0; connection = connect()
    try:
        with connection.cursor() as cursor:
            for symbol in symbols:
                response = requests.get(f"{options.base_url}/stocks/{symbol}", timeout=10)
                if response.status_code != 200:
                    failures.append({"symbol": symbol, "section": "http", "status": response.status_code}); continue
                api = response.json(); cache = json.loads((STOCKS / f"{symbol}.json").read_text(encoding="utf-8"))["data"]
                cursor.execute("SELECT stock_id FROM stocks WHERE symbol=%s", (symbol,)); stock_id = cursor.fetchone()["stock_id"]
                cursor.execute("SELECT trade_date,close,change,change_percent,volume FROM stock_quotes WHERE stock_id=%s ORDER BY trade_date DESC LIMIT 1", (stock_id,))
                db_quote = cursor.fetchone(); json_quote = cache.get("quote") or {}; api_quote = api.get("quote") or {}
                for field in ("trade_date", "close", "change", "change_percent", "volume"):
                    compare(failures, symbol, "quote", field, api_quote.get(field), db_quote.get(field) if db_quote else None, json_quote.get(field)); checks += 1
                cursor.execute("SELECT valuation_date,pe,pb,dividend_yield FROM stock_valuations WHERE stock_id=%s ORDER BY valuation_date DESC LIMIT 1", (stock_id,))
                db_val = cursor.fetchone(); api_val = api.get("valuation") or {}; json_val = cache.get("valuation") or {}
                for field in ("pe", "pb", "dividend_yield"):
                    cached = json_val.get(field) or {}; cached = cached.get("value") if isinstance(cached, dict) else cached
                    compare(failures, symbol, "valuation", field, api_val.get(field), db_val.get(field) if db_val else None, cached); checks += 1
                cursor.execute("SELECT * FROM stock_chips WHERE stock_id=%s ORDER BY trade_date DESC LIMIT 1", (stock_id,))
                db_chips = cursor.fetchone(); api_chips = (api.get("chips") or {}).get("summary", {}).get("latest") or {}
                history = (cache.get("chips") or {}).get("history") or []; json_chips = history[-1] if history else {}
                for field in ("trade_date", "foreign_net", "investment_trust_net", "dealer_net", "institutional_total", "margin_balance", "short_balance"):
                    compare(failures, symbol, "chips", field, api_chips.get(field), db_chips.get(field) if db_chips else None, json_chips.get(field)); checks += 1
                financial_response = requests.get(f"{options.base_url}/stocks/{symbol}/financials?limit=12", timeout=10)
                if financial_response.status_code != 200:
                    failures.append({"symbol": symbol, "section": "financials_http", "status": financial_response.status_code}); continue
                api_financials = financial_response.json()["financials"]
                cursor.execute("SELECT fiscal_year,quarter,period_end,available_date,eps,revenue,gross_margin,operating_margin,net_margin,roe,debt_ratio,current_ratio,operating_cash_flow,capital_expenditure,free_cash_flow FROM stock_financials WHERE stock_id=%s ORDER BY period_end DESC LIMIT 12", (stock_id,))
                db_financials = cursor.fetchall()
                compare(failures, symbol, "financials", "period_count", len(api_financials), len(db_financials)); checks += 1
                for api_row, db_row in zip(api_financials, db_financials):
                    for field in ("fiscal_year", "quarter", "period_end", "available_date", "eps", "revenue", "gross_margin", "operating_margin", "net_margin", "roe", "debt_ratio", "current_ratio", "operating_cash_flow", "capital_expenditure", "free_cash_flow"):
                        compare(failures, symbol, "financials", field, api_row.get(field), db_row.get(field)); checks += 1
                financial_path = STOCKS / "financials" / f"{symbol}.json"
                if financial_path.exists():
                    json_rows = list(reversed(json.loads(financial_path.read_text(encoding="utf-8"))["data"]["quarters"]))[:12]
                    for api_row, json_row in zip(api_financials, json_rows):
                        for field in ("period_end", "available_date", "eps", "revenue", "gross_margin", "operating_margin", "net_margin", "roe", "debt_ratio", "current_ratio", "operating_cash_flow", "capital_expenditure", "free_cash_flow"):
                            compare(failures, symbol, "financial_json", field, api_row.get(field), json_row.get(field)); checks += 1
    finally:
        connection.close()
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "base_url": options.base_url, "symbols": symbols, "symbol_count": len(symbols),
              "checks": checks, "success_count": checks - len(failures), "failure_count": len(failures),
              "failures": failures, "status": "passed" if not failures else "failed"}
    atomic(REPORT, report); print(json.dumps({key: report[key] for key in ("status","symbol_count","checks","failure_count")}))
    return 0 if not failures else 1


if __name__ == "__main__": raise SystemExit(main())
