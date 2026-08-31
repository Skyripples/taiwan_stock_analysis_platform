"""V3.14 Phase 2: leakage-safe, cross-stock T+1 prediction research pilot.

The script scans only the selected symbols/columns from the local Parquet lake.
It intentionally does not touch PostgreSQL or any production model artifact.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import psutil
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, brier_score_loss, f1_score, log_loss,
                             precision_score, recall_score, roc_auc_score)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[1]
LAKE = ROOT / "data_lake"
OUT = ROOT / "data" / "analysis" / "current" / "v314_stock_prediction_pilot.json"
FIXED = ["2330", "2317", "6488", "2881", "1101"]
SEED = 31402
TRAIN_END = "2024-12-31"
TEST_START = "2025-01-01"
VERSION = "v3.14-1.0"


def glob(name: str) -> str:
    return str(LAKE / name / "**" / "*.parquet").replace("\\", "/")


def metric(y: np.ndarray, probability: np.ndarray) -> dict:
    prediction = (probability >= .5).astype(int)
    result = {
        "samples": int(len(y)), "accuracy": float(accuracy_score(y, prediction)),
        "precision": float(precision_score(y, prediction, zero_division=0)),
        "recall": float(recall_score(y, prediction, zero_division=0)),
        "f1": float(f1_score(y, prediction, zero_division=0)),
        "roc_auc": float(roc_auc_score(y, probability)) if len(set(y)) == 2 else None,
        "brier": float(brier_score_loss(y, probability)),
        "log_loss": float(log_loss(y, probability, labels=[0, 1])),
    }
    return result


def confidence_buckets(y: np.ndarray, p: np.ndarray) -> dict:
    pred = (p >= .5).astype(int)
    confidence = np.maximum(p, 1 - p)
    out = {}
    for threshold in (.60, .65, .70):
        mask = confidence > threshold
        out[f"gt_{int(threshold*100)}pct"] = {
            "samples": int(mask.sum()),
            "coverage": float(mask.mean()),
            "accuracy": float(accuracy_score(y[mask], pred[mask])) if mask.any() else None,
        }
    return out


def calibration(y: np.ndarray, p: np.ndarray) -> dict:
    bins = []
    weighted = 0.0
    for low in np.arange(0, 1, .1):
        high = low + .1
        mask = (p >= low) & ((p < high) if high < 1 else (p <= high))
        actual = float(y[mask].mean()) if mask.any() else None
        average = float(p[mask].mean()) if mask.any() else None
        error = abs(actual-average) if mask.any() else None
        weighted += (mask.sum()/len(y))*error if mask.any() else 0
        bins.append({"range": f"{int(low*100)}-{int(high*100)}", "samples": int(mask.sum()),
                     "mean_probability": average, "actual_up_rate": actual, "absolute_error": error})
    return {"ece_10_bins": float(weighted), "bins": bins}


def model_pipeline(numeric: list[str], categorical: list[str]) -> Pipeline:
    transformers = [("numeric", Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
        ("scaler", StandardScaler()),
    ]), numeric)]
    if categorical:
        transformers.append(("categorical", OneHotEncoder(handle_unknown="ignore"), categorical))
    return Pipeline([
        ("preprocess", ColumnTransformer(transformers, remainder="drop")),
        ("model", LogisticRegression(max_iter=500, solver="liblinear", random_state=SEED)),
    ])


def fit_experiment(train: pd.DataFrame, test: pd.DataFrame, numeric: list[str], categorical: list[str]) -> tuple[dict, np.ndarray]:
    model = model_pipeline(numeric, categorical)
    columns = numeric + categorical
    model.fit(train[columns], train["target_direction"].astype(int))
    probability = model.predict_proba(test[columns])[:, 1]
    result = metric(test["target_direction"].to_numpy(dtype=int), probability)
    return result, probability


def main() -> None:
    started = time.perf_counter()
    process = psutil.Process()
    baseline_rss = process.memory_info().rss
    peak_holder = [baseline_rss]
    stop_sample = threading.Event()
    def sample_memory() -> None:
        while not stop_sample.wait(.05):
            try: peak_holder[0] = max(peak_holder[0], process.memory_info().rss)
            except psutil.Error: return
    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    db = duckdb.connect()
    db.execute("SET threads=4")
    db.execute("SET memory_limit='4GB'")
    db.execute(f"CREATE VIEW features AS SELECT * FROM read_parquet('{glob('stock_prediction_features')}', hive_partitioning=true, union_by_name=true)")
    db.execute(f"CREATE VIEW targets AS SELECT * FROM read_parquet('{glob('stock_prediction_targets')}', hive_partitioning=true, union_by_name=true)")
    db.execute(f"CREATE VIEW stocks AS SELECT * FROM read_parquet('{glob('dimensions/stocks')}', hive_partitioning=true, union_by_name=true)")

    schema = db.execute("DESCRIBE features").fetchall()
    columns = [row[0] for row in schema]
    excluded = {"market", "partition_year", "symbol", "feature_date", "target_date", "feature_version",
                "feature_available_cutoff", "availability_json"}
    all_features = [c for c in columns if c not in excluded]
    global_candidates = {
        "global_nasdaq100_change_percent", "global_russell2000_change_percent", "global_ewt_change_percent",
        "global_vix_change_percent", "global_vix_level", "global_copper_change_percent",
        "global_wti_change_percent", "global_gold_change_percent", "global_usdjpy_change_percent",
        "global_usdkrw_change_percent", "global_us5y_change", "global_us30y_change",
        "global_spread_10y_3m", "global_spread_10y_3m_change", "global_btc_return_1h",
        "global_btc_return_4h", "global_btc_return_12h", "global_btc_return_24h",
        "global_eth_return_24h", "global_initial_claims_value", "global_initial_claims_change",
        "global_initial_claims_yoy",
    }
    global_features = [c for c in all_features if c in global_candidates or c.startswith("market_")]
    industry_features = [c for c in all_features if c.startswith("industry_") or c == "stock_vs_industry_return"]
    stock_features = [c for c in all_features if not c.startswith("global_") and not c.startswith("market_")
                      and c not in set(industry_features)]
    projected_features = stock_features + industry_features + global_features

    universe = db.execute("""
        SELECT s.symbol, s.market, s.industry
        FROM stocks s JOIN (SELECT DISTINCT symbol FROM features WHERE feature_version=?) f USING(symbol)
        WHERE lower(coalesce(s.instrument_type,'')) IN ('stock','common_stock','ordinary_stock','ordinary')
           OR (length(s.symbol)=4 AND regexp_matches(s.symbol, '^[0-9]{4}$') AND s.symbol NOT LIKE '00%')
        ORDER BY s.symbol
    """, [VERSION]).fetchall()
    available = {row[0]: row for row in universe}
    missing_fixed = [s for s in FIXED if s not in available]
    if missing_fixed:
        raise RuntimeError(f"Pilot fixed symbols missing: {missing_fixed}")
    candidates = [row[0] for row in universe if row[0] not in FIXED]
    selected = FIXED + random.Random(SEED).sample(candidates, 45)

    select_features = ",".join(f'f."{c}"' for c in projected_features)
    placeholders = ",".join("?" for _ in selected)
    query = f"""
      SELECT f.symbol,f.feature_date,f.target_date,f.feature_available_cutoff,f.availability_json,
             s.market,coalesce(s.industry,'unknown') industry,{select_features},
             t.target_direction,t.target_return
      FROM features f
      JOIN targets t USING(symbol,feature_date)
      JOIN stocks s USING(symbol)
      WHERE f.feature_version=? AND t.target_version=? AND t.horizon=1
        AND f.symbol IN ({placeholders})
      ORDER BY f.feature_date,f.symbol
    """
    frame = db.execute(query, [VERSION, VERSION, *selected]).fetch_df()
    if frame.empty:
        raise RuntimeError("DuckDB returned no pilot rows")
    loaded_rows = len(frame)

    # Full-row audit: every recorded source timestamp must precede the persisted 09:00 cutoff.
    leakage = db.execute(f"""
      SELECT count(*) FROM features f, json_each(f.availability_json) a
      WHERE f.feature_version=? AND f.symbol IN ({placeholders})
        AND try_cast(json_extract_string(a.value,'$') AS TIMESTAMPTZ) >= f.feature_available_cutoff
    """, [VERSION, *selected]).fetchone()[0]
    if leakage:
        raise RuntimeError(f"Temporal leakage detected: {leakage}")

    for c in projected_features:
        frame[c] = pd.to_numeric(frame[c], errors="coerce")
    # Features absent for every training row cannot be imputed and carry no pilot information.
    # Prevent structural pre-inception missingness from teaching the model a fake era signal.
    # The first sustained date with >=80% of direct global candidates available defines the
    # common research window; this is selected without looking at the target.
    direct_global = [c for c in global_features if c.startswith("global_")]
    daily_coverage = frame.groupby("feature_date")[direct_global].apply(lambda x: float(x.notna().mean().mean()))
    sustained = daily_coverage.rolling(20, min_periods=20).mean()
    eligible = sustained[sustained >= .80]
    if eligible.empty:
        raise RuntimeError("No sustained global-feature research window with >=80% availability")
    research_start = eligible.index[0]
    frame = frame[frame.feature_date >= research_start].copy()
    train = frame[frame.feature_date.astype(str) <= TRAIN_END].copy()
    test = frame[frame.feature_date.astype(str) >= TEST_START].copy()
    usable = [c for c in projected_features if train[c].notna().any()]
    stock_used = [c for c in stock_features if c in usable]
    industry_used = [c for c in industry_features if c in usable]
    global_used = [c for c in global_features if c in usable]
    if train.empty or test.empty or train.target_direction.nunique() != 2:
        raise RuntimeError("Strict temporal split has insufficient train/test data")

    experiments = {}
    probabilities = {}
    specifications = {
        "stock_only": (stock_used, []),
        "stock_plus_industry": (stock_used + industry_used, []),
        "stock_plus_industry_global": (stock_used + industry_used + global_used, []),
        "full_plus_symbol_industry_encoding": (stock_used + industry_used + global_used, ["symbol", "industry"]),
    }
    for name, (numeric, categorical) in specifications.items():
        experiments[name], probabilities[name] = fit_experiment(train, test, numeric, categorical)
        experiments[name]["numeric_feature_count"] = len(numeric)
        experiments[name]["categorical_features"] = categorical
    primary_name = "full_plus_symbol_industry_encoding"
    p = probabilities[primary_name]
    y = test.target_direction.to_numpy(dtype=int)

    always_up = np.ones(len(test))
    previous_direction = (test["return_1d"].fillna(0).to_numpy() > 0).astype(float)
    baselines = {"always_up": metric(y, always_up), "previous_day_direction": metric(y, previous_direction)}

    by_year = {}
    for year in sorted(test.feature_date.dt.year.unique()):
        mask = test.feature_date.dt.year.to_numpy() == year
        by_year[str(year)] = metric(y[mask], p[mask])
    by_stock = {}
    for symbol in selected:
        mask = test.symbol.to_numpy() == symbol
        if mask.any(): by_stock[symbol] = metric(y[mask], p[mask])
    accuracies = [v["accuracy"] for v in by_stock.values()]

    # Turnover is unavailable as a direct market-cap proxy in this feature lake; use training-period
    # median turnover_ratio_20d as a transparent liquidity/size proxy.
    size_proxy = "turnover_ratio_20d" if "turnover_ratio_20d" in train else "volume_ratio_20d"
    stock_liquidity = train.groupby("symbol")[size_proxy].median().sort_values()
    midpoint = stock_liquidity.median()
    low_symbols = set(stock_liquidity[stock_liquidity <= midpoint].index)
    low_mask = test.symbol.isin(low_symbols).to_numpy()
    size_groups = {
        "lower_liquidity_proxy": metric(y[low_mask], p[low_mask]),
        "higher_liquidity_proxy": metric(y[~low_mask], p[~low_mask]),
        "warning": "Liquidity proxy is not market capitalization; large/small-cap inference is unavailable without point-in-time market cap.",
    }

    primary_numeric = stock_used + industry_used + global_used
    missing = {c: float(frame[c].isna().mean()) for c in primary_numeric}
    runtime = time.perf_counter() - started
    stop_sample.set(); sampler.join()
    peak = max(peak_holder[0], process.memory_info().rss)
    report = {
        "phase": "V3.14 Phase 2 Full-market stock T+1 prediction pilot",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "data_source": "local Parquet data_lake queried with DuckDB",
        "pilot": {"seed": SEED, "symbols": selected, "fixed_symbols": FIXED, "random_symbols": selected[5:],
                  "stocks": len(selected), "rows": len(frame), "train_rows": len(train), "test_rows": len(test),
                  "research_start": str(research_start.date()),
                  "research_window_rule": "first 20-day sustained window with >=80% direct-global availability",
                  "train_range": [str(train.feature_date.min().date()), str(train.feature_date.max().date())],
                  "test_range": [str(test.feature_date.min().date()), str(test.feature_date.max().date())]},
        "target": "T+1 close > feature-date close",
        "temporal_validation": {"split": f"train <= {TRAIN_END}; test >= {TEST_START}", "random_split": False,
                                "availability_rule": "available_at < target_date 09:00 Asia/Taipei",
                                "audited_rows": loaded_rows, "research_window_rows": len(frame),
                                "leakage_count": int(leakage), "passed": leakage == 0},
        "features": {"lake_columns": len(all_features), "used": len(primary_numeric), "stock_specific": stock_used,
                     "industry": industry_used, "global": global_used,
                     "all_null_in_training_excluded": sorted(set(projected_features)-set(usable)),
                     "redundant_global_derivatives_not_selected": sorted(set(c for c in all_features if c.startswith('global_'))-set(global_used)),
                     "missing_rate": missing, "preprocessing": "train-only median imputation (+ missing indicators), StandardScaler; one-hot encoding for symbol/industry in full experiment"},
        "baselines": baselines, "experiments": experiments,
        "primary_model": {"name": primary_name, **experiments[primary_name],
                          "confidence": confidence_buckets(y, p), "calibration": calibration(y, p)},
        "annual_stability": by_year,
        "stock_performance": {"per_symbol": by_stock, "distribution": {"count": len(accuracies),
            "min_accuracy": min(accuracies), "p25_accuracy": float(np.percentile(accuracies,25)),
            "median_accuracy": float(np.median(accuracies)), "p75_accuracy": float(np.percentile(accuracies,75)),
            "max_accuracy": max(accuracies), "above_always_up_count": sum(v["accuracy"] > baselines["always_up"]["accuracy"] for v in by_stock.values())}},
        "size_liquidity_analysis": size_groups,
        "resource_usage": {"runtime_seconds": runtime, "baseline_rss_bytes": baseline_rss,
                           "peak_rss_bytes": int(peak), "duckdb_memory_limit": "4GB", "entire_lake_loaded": False},
        "conclusions": {
            "global_model_cross_stock_generalization": None,
            "stock_encoding_delta_accuracy": experiments[primary_name]["accuracy"]-experiments["stock_plus_industry_global"]["accuracy"],
            "industry_feature_delta_accuracy": experiments["stock_plus_industry"]["accuracy"]-experiments["stock_only"]["accuracy"],
            "global_feature_delta_accuracy": experiments["stock_plus_industry_global"]["accuracy"]-experiments["stock_plus_industry"]["accuracy"],
        },
    }
    report["conclusions"]["global_model_cross_stock_generalization"] = (
        report["primary_model"]["roc_auc"] > .5 and report["primary_model"]["accuracy"] > baselines["always_up"]["accuracy"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    os.replace(tmp, OUT)
    print(json.dumps({"output": str(OUT), "rows": len(frame), "features": len(primary_numeric),
                      "primary": report["primary_model"], "runtime_seconds": runtime}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
