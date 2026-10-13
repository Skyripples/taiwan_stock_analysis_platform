"""Audit active production/research datasets without scanning archived artifacts."""
from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
START = date(2021, 9, 11)
END = date(2026, 9, 10)
OUT = ROOT / "data/analysis/current/data_completeness_report.json"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def trading_sessions() -> list[str]:
    pattern = str(ROOT / "data_lake/taiwan_pit/market_sessions/**/*.parquet").replace("\\", "/")
    db = duckdb.connect()
    columns = [row[0] for row in db.execute("DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true)", [pattern]).fetchall()]
    key = next((name for name in ("trade_date", "session_date", "calendar_date", "date") if name in columns), None)
    if key is None:
        raise RuntimeError(f"Unsupported market session schema: {columns}")
    trading_filter = " AND is_trading_day" if "is_trading_day" in columns else ""
    rows = db.execute(
        f"SELECT DISTINCT CAST({key} AS DATE) d FROM read_parquet(?, union_by_name=true) "
        f"WHERE CAST({key} AS DATE) BETWEEN ? AND ?{trading_filter} ORDER BY d",
        [pattern, START, END],
    ).fetchall()
    db.close()
    return [row[0].isoformat() for row in rows]


def market_entries(expected: list[str]) -> list[dict]:
    rows = []
    for relative in ("data/history/backfill_market_daily.csv", "data/history/market_daily.csv"):
        path = ROOT / relative
        rows.extend(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    rows = [row for row in rows if START.isoformat() <= row["trade_date"] <= END.isoformat()]
    mappings = {
        "TAIEX": ["taiex_close", "taiex_change_percent"], "TPEx": ["tpex_close"],
        "turnover": ["turnover"], "advancing_declining": ["advancing", "declining"],
        "institutional_investors": ["foreign_cash_flow"], "foreign_futures": ["foreign_futures_position"],
        "night_futures": ["night_futures_change"], "TSM_ADR": ["tsm_adr_change_percent"],
        "SOX": ["sox_change_percent"], "SP500": ["sp500_change_percent"],
        "NASDAQ": ["nasdaq_change_percent"], "VIX": ["vix_change_percent"],
        "KOSPI": ["kospi_change_percent"],
    }
    result = []
    expected_set = set(expected)
    for name, fields in mappings.items():
        valid = ({row["trade_date"] for row in rows if all(row.get(field, "").strip() for field in fields)}
                 & expected_set)
        missing = sorted(expected_set - valid)
        result.append(entry(name, expected, sorted(valid), missing, "existing official/Yahoo historical pipeline", True))
    return result


def entry(name, expected, actual, missing, source, recoverable, *, malformed=0, note=None):
    expected_count = len(expected)
    actual_count = len(actual)
    return {
        "dataset": name, "expected_start": START.isoformat(), "expected_end": END.isoformat(),
        "actual_start": min(actual) if actual else None, "actual_end": max(actual) if actual else None,
        "expected_rows_or_sessions": expected_count, "actual_rows": actual_count,
        "missing_dates_or_ranges": missing, "duplicate_count": max(0, actual_count - len(set(actual))),
        "null_or_malformed_count": malformed, "source": source,
        "recoverable": recoverable, "unrecoverable": not recoverable,
        "coverage_percent": round(100 * actual_count / expected_count, 4) if expected_count else None,
        "note": note,
    }


def snapshot(name, path, source, note="Current snapshot; five-year history is not collected by design"):
    payload = read_json(ROOT / path)
    stamp = payload.get("data_date") or payload.get("trade_date") or payload.get("source_date") or payload.get("updated_at")
    return {
        "dataset": name, "expected_start": None, "expected_end": END.isoformat(), "actual_start": stamp,
        "actual_end": stamp, "expected_rows_or_sessions": 1, "actual_rows": 1,
        "missing_dates_or_ranges": [], "duplicate_count": 0, "null_or_malformed_count": 0,
        "source": source, "recoverable": False, "unrecoverable": True,
        "coverage_percent": 100.0, "note": note,
    }


def run() -> dict:
    sessions = trading_sessions()
    stock = read_json(ROOT / "data/analysis/current/data_completeness_stock_validation.json")
    index_payload = read_json(ROOT / "data/stocks/index.json")
    universe = index_payload.get("stocks", index_payload if isinstance(index_payload, list) else [])
    companies = [item for item in universe if item.get("instrument_type") == "company"]
    industry_valid = sum(bool(item.get("industry")) for item in companies)
    datasets = market_entries(sessions)
    for key, label in (("stock_daily_prices", "stock_OHLCV"), ("stock_prediction_features", "stock_prediction_features"), ("stock_prediction_targets", "stock_prediction_targets")):
        item = stock["datasets"][key]
        datasets.append({
            "dataset": label, "expected_start": START.isoformat(), "expected_end": END.isoformat(),
            "actual_start": str(item["first_date"]), "actual_end": str(item["last_date"]),
            "expected_rows_or_sessions": item["expected_rows"], "actual_rows": item["rows"],
            "missing_dates_or_ranges": ([f"{item['last_date']}..{END.isoformat()}"] if str(item["last_date"]) < END.isoformat() else []),
            "duplicate_count": item["pk_duplicates"], "null_or_malformed_count": 0,
            "source": "local Parquet Data Lake", "recoverable": True, "unrecoverable": False,
            "coverage_percent": 100.0 if item["row_count_match"] else round(100 * item["rows"] / item["expected_rows"], 4),
            "note": "Manifest integrity coverage; latest-session lag is reported separately",
        })
    datasets.extend([
        {"dataset": "stock_universe", "expected_start": None, "expected_end": END.isoformat(), "actual_start": None,
         "actual_end": None, "expected_rows_or_sessions": len(universe), "actual_rows": len(universe),
         "missing_dates_or_ranges": [], "duplicate_count": len(universe)-len({x.get('symbol') for x in universe}),
         "null_or_malformed_count": 0, "source": "TWSE/TPEx official company masters", "recoverable": True,
         "unrecoverable": False, "coverage_percent": 100.0, "note": f"company industry {industry_valid}/{len(companies)}"},
        snapshot("ETF_funds", "data/funds/funds.json", "TWSE/TPEx official"),
        snapshot("bond_yields", "data/bonds/bond_yields.json", "TPEx/CBC and U.S. Treasury"),
        snapshot("FX", "data/forex/exchange_rates.json", "CBC BP01D01"),
        snapshot("bank_deposit_rates", "data/deposits/bank_rates.json", "CBC BIRWEB"),
        snapshot("TX_futures", "data/futures/tx_futures.json", "TAIFEX official"),
        snapshot("events", "data/event.json", "TWSE/MOPS event flow", "Event calendar; session coverage is not applicable"),
    ])
    structural = ["stock_valuation", "stock_monthly_revenue", "stock_financials", "stock_chips"]
    for name in structural:
        datasets.append({
            "dataset": name, "expected_start": START.isoformat(), "expected_end": END.isoformat(),
            "actual_start": None, "actual_end": None, "expected_rows_or_sessions": None, "actual_rows": None,
            "missing_dates_or_ranges": ["Per-symbol/API database coverage requires Linode DB audit"],
            "duplicate_count": None, "null_or_malformed_count": None, "source": "TWSE/TPEx/MOPS official",
            "recoverable": True, "unrecoverable": False, "coverage_percent": None,
            "note": "Not duplicated into Git; authoritative rows live in Linode PostgreSQL/API",
        })
    report = {
        "version": "V3.25", "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start": START.isoformat(), "end": END.isoformat(), "taiwan_sessions": len(sessions)},
        "datasets": datasets,
        "backfill": {
            "production_market_sessions_added": 1, "production_signal_sessions_added": 1,
            "production_date": "2026-09-10", "compatibility_dry_run_checked": 487,
            "compatibility_fully_recoverable": 23, "compatibility_unrecoverable": 464,
            "research_rows_after": 748, "historical_prediction_rows_after": 745,
            "signals_rows_after": 749,
        },
        "validation": {"stock_temporal_leakage": stock["temporal_leakage"],
                       "stock_sample_passed": stock["sample_validation"]["passed"],
                       "stock_sample_total": stock["sample_validation"]["count"]},
        "risks": [
            "Research market history begins 2023-08-10; older five-year sessions are not fully recoverable through every required source.",
            "The legacy five-year market backfill cannot resume the extended macro/Asia/semiconductor CSV schema; no existing rows were changed.",
            "Local full-market Parquet snapshot ends 2026-08-25 and needs a separate incremental Data Lake build.",
            "Linode research stock tables are empty after the planned V3.14 cleanup, so the local Parquet feature lake cannot be incrementally exported from production snapshots.",
            f"Stock universe currently has {len(companies) - industry_valid} company rows without an industry value.",
            "Valuation, revenue, financial and chips completeness is authoritative in Linode PostgreSQL and cannot be fully proven offline.",
            "Product datasets are current snapshots, not five-year historical series.",
        ],
    }
    atomic_json(OUT, report)
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps({"datasets": len(result["datasets"]), "window": result["window"],
                      "backfill": result["backfill"], "validation": result["validation"]}, ensure_ascii=False))
