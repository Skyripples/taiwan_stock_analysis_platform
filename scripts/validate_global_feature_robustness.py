"""V3.13 Phase 4 robustness validation for selected global candidates.

Research only: this module never writes production model artifacts, calibration,
prediction output, frontend data, or registry state.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


LOGGER = logging.getLogger("global_feature_robustness")
INPUT = PROJECT_ROOT / "data" / "analysis" / "global_taiex_feature_dataset.json.gz"
OUTPUT = PROJECT_ROOT / "data" / "analysis" / "global_feature_robustness.json"
INITIAL = 250
NEAR_ZERO = 0.05
CANDIDATES = {
    "ethusd_return_24h": "eth_return_24h",
    "us5y_change": "us5y_change",
    "russell2000_change_percent": "russell2000_change_percent",
    "initial_claims_yoy": "initial_claims_yoy",
}
EXPERIMENTS = {
    "A_formal_15": (),
    "B_plus_eth24h": ("eth_return_24h",),
    "C_plus_us5y": ("us5y_change",),
    "D_plus_russell2000": ("russell2000_change_percent",),
    "E_plus_initial_claims_yoy": ("initial_claims_yoy",),
    "F_eth24h_plus_us5y": ("eth_return_24h", "us5y_change"),
    "G_eth24h_plus_russell2000": ("eth_return_24h", "russell2000_change_percent"),
    "H_us5y_plus_russell2000": ("us5y_change", "russell2000_change_percent"),
}
METRICS = ("accuracy", "precision", "recall", "f1", "roc_auc", "brier", "log_loss", "ece")


def _load():
    with gzip.open(INPUT, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    formal = tuple(payload["formal_features"])
    required = (*formal, *CANDIDATES.values(), "feature_date", "target_date", "target_direction", "taiex_change_percent", "vix_level")
    rows = [row for row in payload["rows"] if all(row.get(key) is not None and (not isinstance(row.get(key), float) or math.isfinite(row[key])) for key in required)]
    if len(rows) <= INITIAL:
        raise ValueError("Insufficient complete cases")
    return rows, formal


def _ece(actual: list[int], probabilities: list[float], bins: int = 10) -> float:
    total = len(actual); result = 0.0
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        positions = [i for i, value in enumerate(probabilities) if lower <= value < upper or (index == bins - 1 and value == 1)]
        if positions:
            forecast = statistics.mean(probabilities[i] for i in positions)
            observed = statistics.mean(actual[i] for i in positions)
            result += len(positions) / total * abs(forecast - observed)
    return result


def _metrics(actual, predicted, probabilities, hits):
    from sklearn.metrics import accuracy_score, brier_score_loss, f1_score, log_loss, precision_score, recall_score, roc_auc_score
    return {
        "samples": len(actual), "accuracy": _clean(accuracy_score(actual, predicted)),
        "precision": _clean(precision_score(actual, predicted, zero_division=0)),
        "recall": _clean(recall_score(actual, predicted, zero_division=0)),
        "f1": _clean(f1_score(actual, predicted, zero_division=0)),
        "roc_auc": _clean(roc_auc_score(actual, probabilities)) if len(set(actual)) == 2 else None,
        "brier": _clean(brier_score_loss(actual, probabilities)),
        "log_loss": _clean(log_loss(actual, probabilities, labels=[0, 1])),
        "ece": _clean(_ece(actual, probabilities)),
        "recent_50_accuracy": _clean(sum(hits[-50:]) / min(50, len(hits))),
        "recent_100_accuracy": _clean(sum(hits[-100:]) / min(100, len(hits))),
    }


def _walk(rows, features, candidate_features):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    actual=[]; predicted=[]; probabilities=[]; hits=[]; records=[]
    coefficients={key:[] for key in candidate_features}
    selected=tuple(features)
    for index in range(INITIAL, len(rows)):
        train=rows[:index]; current=rows[index]
        model=Pipeline([("scaler",StandardScaler()),("classifier",LogisticRegression(max_iter=1000,random_state=42))])
        model.fit([[row[key] for key in selected] for row in train],[row["target_direction"] for row in train])
        sample=[[current[key] for key in selected]]; pred=int(model.predict(sample)[0])
        class_positions={int(label):position for position,label in enumerate(model.classes_)}
        probability=float(model.predict_proba(sample)[0][class_positions[1]]); truth=int(current["target_direction"])
        hit=pred==truth; actual.append(truth); predicted.append(pred); probabilities.append(probability); hits.append(hit)
        for key in candidate_features:
            coefficients[key].append(float(model.named_steps["classifier"].coef_[0][selected.index(key)]))
        records.append({"feature_date":current["feature_date"],"target_date":current["target_date"],"actual":truth,
            "prediction":pred,"up_probability":probability,"hit":hit,"vix_level":float(current["vix_level"]),
            "taiex_change_percent":float(current["taiex_change_percent"])})
    result=_metrics(actual,predicted,probabilities,hits)
    result["feature_count"]=len(selected); result["features"]=list(selected); result["prediction_count"]=len(records)
    result["predictions"]=records; result["coefficient_stability"]={key:_coefficient_stats(values) for key,values in coefficients.items()}
    return result


def _coefficient_stats(values):
    flips=sum(1 for left,right in zip(values,values[1:]) if left*right<0)
    return {"observations":len(values),"median":_clean(statistics.median(values)),"mean":_clean(statistics.mean(values)),
        "min":_clean(min(values)),"max":_clean(max(values)),"sign_flips":flips,
        "near_zero_threshold":NEAR_ZERO,"near_zero_ratio":_clean(sum(abs(value)<NEAR_ZERO for value in values)/len(values)),
        "positive_ratio":_clean(sum(value>0 for value in values)/len(values))}


def _subset_metrics(records):
    actual=[r["actual"] for r in records]; predicted=[r["prediction"] for r in records]
    probabilities=[r["up_probability"] for r in records]; hits=[r["hit"] for r in records]
    return _metrics(actual,predicted,probabilities,hits) if records else {"samples":0,"status":"unavailable"}


def _regimes(rows, results):
    vix_median=statistics.median(row["vix_level"] for row in rows[INITIAL:])
    changes=[float(row["taiex_change_percent"]) for row in rows]
    volatility=[statistics.stdev(changes[index-19:index+1]) if index>=19 else None for index in range(len(rows))]
    vol_median=statistics.median(value for value in volatility[INITIAL:] if value is not None)
    labels=[]
    for index,row in enumerate(rows[INITIAL:],start=INITIAL):
        weekday=datetime.fromisoformat(row["target_date"]).weekday()
        labels.append({"vix_high":row["vix_level"]>=vix_median,"taiex_high_volatility":volatility[index]>=vol_median,
            "previous_taiex_up":row["taiex_change_percent"]>0,"monday":weekday==0})
    output={}
    for name,result in results.items():
        output[name]={}
        for key,true_name,false_name in (
            ("vix_high","high","low"),("taiex_high_volatility","high","low"),
            ("previous_taiex_up","up","down_or_flat"),("monday","monday","tue_to_fri")):
            output[name][key]={}
            for expected,label in ((True,true_name),(False,false_name)):
                subset=[record for record,flags in zip(result["predictions"],labels) if flags[key] is expected]
                output[name][key][label]=_subset_metrics(subset)
    return {"thresholds":{"vix_median":_clean(vix_median),"taiex_20d_volatility_median":_clean(vol_median)},"models":output}


def _yearly(result):
    groups=defaultdict(list)
    for row in result["predictions"]: groups[row["target_date"][:4]].append(row)
    return {year:_subset_metrics(records) for year,records in sorted(groups.items()) if year in {"2024","2025","2026"}}


def _view(result):
    return {key:value for key,value in result.items() if key!="predictions"}


def _delta(result,baseline):
    return {key:_clean(result[key]-baseline[key]) for key in METRICS}


def validate():
    rows,formal=_load(); raw={}
    for name,extras in EXPERIMENTS.items(): raw[name]=_walk(rows,(*formal,*extras),extras)
    baseline=raw["A_formal_15"]
    models={name:{**_view(result),"delta_vs_A":_delta(result,baseline),"yearly":_yearly(result)} for name,result in raw.items()}
    regimes=_regimes(rows,raw)
    recommendations=[]
    for name in ("B_plus_eth24h","C_plus_us5y","D_plus_russell2000"):
        delta=models[name]["delta_vs_A"]
        if delta["roc_auc"]>0 and delta["accuracy"]>=0 and delta["brier"]<=0 and delta["log_loss"]<=0:
            recommendations.append(name)
    report={"generated_at":datetime.now(timezone.utc).isoformat(),"version":"v3.13-phase4-1.0","research_only":True,
        "method":{"algorithm":"StandardScaler + LogisticRegression(max_iter=1000, random_state=42)",
            "validation":"expanding-window one-step-ahead","initial_training_size":INITIAL,"dataset_rows":len(rows),
            "oos_predictions":len(rows)-INITIAL,"same_oos_dates":True,"tuning":False},
        "candidate_aliases":CANDIDATES,"experiments":models,"regime_validation":regimes,
        "experimental_macro":{"feature":"initial_claims_yoy","status":"not_eligible_for_production",
            "reason":"Current FRED history is revised data, not point-in-time ALFRED vintages."},
        "strict_multi_metric_candidates":recommendations,
        "decision_note":"A candidate should not enter production from small aggregate gains alone; require stable yearly/regime evidence and point-in-time-safe data.",
        "production_artifacts_modified":False}
    _write(report); return report


def _clean(value):
    result=float(value)
    if not math.isfinite(result): raise ValueError("Non-finite result")
    return round(result,12)


def _write(payload):
    OUTPUT.parent.mkdir(parents=True,exist_ok=True); temporary=OUTPUT.with_name(f".{OUTPUT.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w",encoding="utf-8") as handle:
            json.dump(payload,handle,ensure_ascii=False,indent=2,allow_nan=False); handle.write("\n")
        os.replace(temporary,OUTPUT)
    finally:
        if temporary.exists(): temporary.unlink()


def main():
    logging.basicConfig(level=logging.INFO,format="%(levelname)s | %(message)s")
    try: report=validate()
    except Exception as exc:
        LOGGER.error("Robustness validation failed: %s",exc); return 1
    LOGGER.info("Robustness validation complete | experiments=%d | OOS=%d",len(report["experiments"]),report["method"]["oos_predictions"]); return 0


if __name__=="__main__": raise SystemExit(main())
