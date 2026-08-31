"""Validate Taiwan PIT Parquet coverage, formulas, keys, and availability."""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

import duckdb

from config import PROJECT_ROOT


ROOT = PROJECT_ROOT / "data_lake" / "taiwan_pit"
REPORT = ROOT / "validation_report.json"


def glob(name: str) -> str:
    return str(ROOT / name / "**" / "*.parquet").replace("\\", "/")


def main() -> int:
    connection = duckdb.connect()
    try:
        datasets = {}
        definitions = {
            "market_sessions": ("calendar_date", "market,calendar_date"),
            "issued_shares": ("trade_date", "market,symbol,trade_date"),
            "corporate_actions": ("ex_date", "market,symbol,ex_date,action_type"),
            "daily_market_cap": ("trade_date", "market,symbol,trade_date"),
        }
        for name, (date_field, keys) in definitions.items():
            files = list((ROOT / name).rglob("*.parquet"))
            if not files:
                datasets[name] = {"rows": 0, "first_date": None, "last_date": None, "duplicates": 0}
                continue
            row = connection.execute(
                f"SELECT count(*),min({date_field}),max({date_field}),count(*)-count(DISTINCT ({keys})) "
                "FROM read_parquet(?,hive_partitioning=true)", [glob(name)]
            ).fetchone()
            datasets[name] = {"rows": row[0], "first_date": str(row[1]), "last_date": str(row[2]), "duplicates": row[3]}
        formula_failures = connection.execute("""
            SELECT count(*) FROM read_parquet(?,hive_partitioning=true)
            WHERE issued_shares IS NOT NULL AND close IS NOT NULL
              AND abs(market_cap_twd-issued_shares*close) > greatest(1.0,abs(market_cap_twd)*1e-12)
        """, [glob("daily_market_cap")]).fetchone()[0]
        leakage = 0
        for name, date_field in (("issued_shares","trade_date"),("corporate_actions","ex_date"),("daily_market_cap","trade_date")):
            if datasets[name]["rows"]:
                leakage += connection.execute(
                    f"SELECT count(*) FROM read_parquet(?,hive_partitioning=true) WHERE available_at IS NULL "
                    f"OR CAST(available_at AS TIMESTAMPTZ) < CAST({date_field} AS TIMESTAMP) AT TIME ZONE 'Asia/Taipei'",
                    [glob(name)],
                ).fetchone()[0]
        sample = connection.execute("""
            SELECT market,symbol,trade_date,issued_shares,close,market_cap_twd
            FROM read_parquet(?,hive_partitioning=true)
            WHERE issued_shares IS NOT NULL AND close IS NOT NULL USING SAMPLE 5 ROWS
        """, [glob("daily_market_cap")]).fetchall()
        report = {"status":"passed" if not formula_failures and not leakage and not sum(v["duplicates"] for v in datasets.values()) else "failed",
                  "generated_at":datetime.now().astimezone().isoformat(),"datasets":datasets,
                  "formula_failures":formula_failures,"temporal_leakage":leakage,
                  "market_cap_samples":[{"market":r[0],"symbol":r[1],"trade_date":str(r[2]),"issued_shares":r[3],"close":r[4],"market_cap_twd":r[5],"recomputed":r[3]*r[4]} for r in sample]}
        REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False)); return 0 if report["status"]=="passed" else 1
    finally: connection.close()


if __name__ == "__main__": raise SystemExit(main())
