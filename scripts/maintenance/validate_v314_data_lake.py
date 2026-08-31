"""Validate the local V3.14 Parquet lake without materialising it in RAM."""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from config import PROJECT_ROOT
from maintenance.query_v314_data_lake import connect

LAKE = PROJECT_ROOT / "data_lake"
FIXED = ("2330", "2317", "6488", "2881", "1101")


def near(left, right, tolerance=1e-7):
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) <= tolerance * max(1.0, abs(float(left)), abs(float(right)))


def pct(values, index, days):
    return (values[index] / values[index - days] - 1) * 100 if index >= days and values[index - days] else None


def rsi(values, index, window=14):
    if index < window:
        return None
    changes = [values[i] - values[i - 1] for i in range(index - window + 1, index + 1)]
    gains = sum(max(value, 0) for value in changes) / window
    losses = sum(max(-value, 0) for value in changes) / window
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def scalar(db, sql, params=None):
    return db.execute(sql, params or []).fetchone()[0]


def dataset_checks(db, manifest):
    definitions = {
        "stock_daily_prices": ("prices", "symbol,trade_date", "trade_date"),
        "stock_prediction_features": ("features", "symbol,feature_date,feature_version", "feature_date"),
        "stock_prediction_targets": ("targets", "symbol,feature_date,horizon,target_version", "feature_date"),
        "stock_industry_daily_features": ("industry", "industry,trade_date,feature_version", "trade_date"),
    }
    output = {}
    for name, (view, key, date_column) in definitions.items():
        row = db.execute(
            f"SELECT count(*),min({date_column}),max({date_column}) FROM {view}"
        ).fetchone()
        duplicates = scalar(
            db,
            f"SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM {view} GROUP BY {key} HAVING count(*)>1)",
        )
        expected = manifest["datasets"][name]["rows"]
        output[name] = {
            "rows": row[0], "expected_rows": expected, "row_count_match": row[0] == expected,
            "first_date": row[1], "last_date": row[2], "pk_duplicates": duplicates,
        }
    return output


def sample_symbols(db):
    candidates = [row[0] for row in db.execute(
        "SELECT symbol FROM prices GROUP BY symbol HAVING count(*)>=100 ORDER BY symbol"
    ).fetchall()]
    random_values = random.Random(314).sample([s for s in candidates if s not in FIXED], 50)
    return list(FIXED) + random_values


def validate_symbol(db, symbol):
    prices = db.execute(
        "SELECT trade_date,high,low,coalesce(adjusted_close,close) price FROM prices WHERE symbol=? ORDER BY trade_date",
        [symbol],
    ).fetchall()
    by_date = {row[0]: index for index, row in enumerate(prices)}
    feature_date = db.execute(
        "SELECT feature_date FROM features WHERE symbol=? AND feature_date IS NOT NULL ORDER BY feature_date LIMIT 1 OFFSET greatest((SELECT count(*) FROM features WHERE symbol=?)/2,60)",
        [symbol, symbol],
    ).fetchone()[0]
    index = by_date[feature_date]
    feature = db.execute(
        "SELECT return_1d,return_5d,ma20_distance,rsi14,atr14,industry_return_1d,industry_return_5d,stock_vs_industry_return,availability_json,feature_available_cutoff FROM features WHERE symbol=? AND feature_date=?",
        [symbol, feature_date],
    ).fetchone()
    closes = [float(row[3]) for row in prices]
    atr = None
    if index >= 14 and all(prices[i][1] is not None and prices[i][2] is not None for i in range(index - 13, index + 1)):
        true_ranges = [max(float(prices[i][1])-float(prices[i][2]), abs(float(prices[i][1])-closes[i-1]), abs(float(prices[i][2])-closes[i-1])) for i in range(index-13,index+1)]
        atr = statistics.mean(true_ranges)
    expected = (pct(closes,index,1), pct(closes,index,5), (closes[index]/statistics.mean(closes[index-19:index+1])-1)*100, rsi(closes,index), atr)
    technical = all(near(a,b) for a,b in zip(expected,feature[:5]))
    target_rows = db.execute(
        "SELECT horizon,target_date,target_return,target_direction FROM targets WHERE symbol=? AND feature_date=? ORDER BY horizon",
        [symbol, feature_date],
    ).fetchall()
    target_ok = True
    for horizon,target_date,target_return,direction in target_rows:
        expected_return=(closes[index+horizon]/closes[index]-1)*100
        target_ok &= target_date == prices[index+horizon][0] and near(target_return,expected_return) and direction == int(expected_return>0)
    industry_row = db.execute(
        "SELECT i.industry_return_1d,i.industry_return_5d FROM industry i JOIN stocks s ON s.industry=i.industry WHERE s.symbol=? AND i.trade_date=? LIMIT 1",
        [symbol, feature_date],
    ).fetchone()
    industry_ok = bool(industry_row) and near(feature[5],industry_row[0]) and near(feature[6],industry_row[1]) and near(feature[7],expected[1]-float(industry_row[1]))
    availability = json.loads(feature[8] or "{}")
    cutoff = feature[9]
    cutoff_ok = all(datetime.fromisoformat(value) < cutoff for value in availability.values())
    first_date = prices[0][0]
    stock_columns={row[0] for row in db.execute("DESCRIBE stocks").fetchall()}
    listed = db.execute("SELECT listed_date FROM stocks WHERE symbol=?", [symbol]).fetchone()[0] if "listed_date" in stock_columns else None
    return {
        "symbol": symbol, "feature_date": feature_date, "technical": bool(technical),
        "targets": bool(target_ok), "industry": bool(industry_ok), "global_cutoff": bool(cutoff_ok),
        "no_prelisting": listed is None or first_date >= listed, "first_price_date": first_date, "listed_date": listed,
    }


def run(lake):
    started = time.perf_counter()
    manifest = json.loads((lake / "manifest.json").read_text(encoding="utf-8"))
    files = list(lake.rglob("*.parquet"))
    db = connect(lake)
    datasets = dataset_checks(db, manifest)
    target_distribution = [dict(zip(("horizon","rows","up","down"), row)) for row in db.execute(
        "SELECT horizon,count(*),sum(target_direction),count(*)-sum(target_direction) FROM targets GROUP BY horizon ORDER BY horizon"
    ).fetchall()]
    samples = [validate_symbol(db, symbol) for symbol in sample_symbols(db)]
    # Vectorized, non-exploding audit: compare the latest timestamp contained in
    # each row's availability JSON with its prediction cutoff.
    leakage = scalar(db, """
      SELECT count(*) FROM features
      WHERE list_max(list_transform(
        regexp_extract_all(availability_json,'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+'),
        value -> try_cast(value AS TIMESTAMPTZ)
      )) >= feature_available_cutoff
    """)
    nulls = {}
    feature_columns = manifest["feature_columns"]
    for offset in range(0,len(feature_columns),20):
        batch=feature_columns[offset:offset+20]
        row=db.execute("SELECT "+",".join(f"count(*) FILTER(WHERE \"{key}\" IS NULL)" for key in batch)+" FROM features").fetchone()
        nulls.update({key:{"null_rows":value,"missing_rate":value/datasets["stock_prediction_features"]["rows"]} for key,value in zip(batch,row)})
    passed=sum(all(item[key] for key in ("technical","targets","industry","global_cutoff","no_prelisting")) for item in samples)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": {"parquet_files":len(files),"bytes":sum(path.stat().st_size for path in files),"declared_bytes":manifest["total_bytes"]},
        "datasets":datasets,"target_distribution":target_distribution,"feature_missing":nulls,
        "temporal_leakage":leakage,"sample_validation":{"count":len(samples),"passed":passed,"failed":len(samples)-passed,"details":samples},
        "validation_seconds":round(time.perf_counter()-started,3),
    }


if __name__ == "__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--lake",type=Path,default=LAKE);parser.add_argument("--output",type=Path);options=parser.parse_args()
    result=run(options.lake)
    if options.output:options.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
    print(json.dumps({"datasets":result["datasets"],"temporal_leakage":result["temporal_leakage"],"sample_validation":result["sample_validation"]},default=str))
