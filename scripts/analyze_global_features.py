"""Predictive-value research for V3.13 global features (no production writes)."""

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
from typing import Any, Iterable

from config import PROJECT_ROOT
from database.connection import connect


LOGGER = logging.getLogger("global_feature_analysis")
INPUT = PROJECT_ROOT / "data" / "analysis" / "global_taiex_feature_dataset.json.gz"
OUTPUT = PROJECT_ROOT / "data" / "analysis" / "global_feature_validation.json"
INITIAL = 250
PRIMARY = (
    "nasdaq100_change_percent", "russell2000_change_percent", "ewt_change_percent",
    "vix_change_percent", "vix_level", "copper_change_percent", "wti_change_percent",
    "gold_change_percent", "usdjpy_change_percent", "usdkrw_change_percent",
    "us5y_change", "us30y_change", "spread_10y_3m", "spread_10y_3m_change",
    "btc_return_1h", "btc_return_4h", "btc_return_12h", "btc_return_24h",
    "eth_return_24h", "initial_claims_value", "initial_claims_change", "initial_claims_yoy",
)
GROUPS = {
    "A_formal_15": (),
    "B_us_market": ("nasdaq100_change_percent", "russell2000_change_percent", "ewt_change_percent"),
    "C_commodity": ("copper_change_percent", "wti_change_percent", "gold_change_percent"),
    "D_fx": ("usdjpy_change_percent", "usdkrw_change_percent"),
    "E_rates": ("us5y_change", "us30y_change", "spread_10y_3m", "spread_10y_3m_change"),
    "F_crypto": ("btc_return_1h", "btc_return_4h", "btc_return_12h", "btc_return_24h", "eth_return_24h"),
}


def _load():
    with gzip.open(INPUT, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload["rows"]
    return rows, tuple(payload["formal_features"]), tuple(payload["global_features"])


def _rank(values: list[float]) -> list[float]:
    ordered = sorted(range(len(values)), key=values.__getitem__); ranks = [0.0] * len(values); i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and values[ordered[j]] == values[ordered[i]]: j += 1
        average = (i + j - 1) / 2 + 1
        for position in ordered[i:j]: ranks[position] = average
        i = j
    return ranks


def _pearson(x: list[float], y: list[float]) -> float:
    if len(x) < 3: return 0.0
    mx, my = statistics.mean(x), statistics.mean(y)
    numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return numerator / denominator if denominator else 0.0


def _correlation(rows, key, target):
    pairs = [(float(r[key]), float(r[target])) for r in rows if r.get(key) is not None]
    x, y = [p[0] for p in pairs], [p[1] for p in pairs]
    return {"observations": len(pairs), "pearson": _clean(_pearson(x, y)),
        "spearman": _clean(_pearson(_rank(x), _rank(y)))}


def _mi(rows, key, target, discrete):
    from sklearn.feature_selection import mutual_info_classif, mutual_info_regression
    pairs = [(float(r[key]), float(r[target])) for r in rows if r.get(key) is not None]
    if len(pairs) < 10: return None
    x = [[p[0]] for p in pairs]; y = [int(p[1]) if discrete else p[1] for p in pairs]
    function = mutual_info_classif if discrete else mutual_info_regression
    return _clean(function(x, y, random_state=42)[0])


def _complete(rows, features):
    return [row for row in rows if all(row.get(key) is not None and math.isfinite(float(row[key])) for key in features)]


def _walk(rows, features):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, brier_score_loss, f1_score, log_loss, precision_score, recall_score, roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    selected = tuple(features); actual=[]; predicted=[]; probability=[]; dates=[]; hits=[]
    for index in range(INITIAL, len(rows)):
        train = rows[:index]; current = rows[index]
        model = Pipeline([("scaler", StandardScaler()), ("classifier", LogisticRegression(max_iter=1000, random_state=42))])
        model.fit([[r[k] for k in selected] for r in train], [r["target_direction"] for r in train])
        sample = [[current[k] for k in selected]]; pred = int(model.predict(sample)[0])
        positions = {int(label): pos for pos, label in enumerate(model.classes_)}
        prob = float(model.predict_proba(sample)[0][positions[1]]); truth = int(current["target_direction"])
        actual.append(truth); predicted.append(pred); probability.append(prob); dates.append(current["target_date"]); hits.append(pred == truth)
    yearly=defaultdict(list)
    for d,h in zip(dates,hits): yearly[d[:4]].append(h)
    return {"sample_rows": len(rows), "prediction_count": len(actual), "features": list(selected),
        "date_range": {"first": dates[0], "last": dates[-1]}, "accuracy": _clean(accuracy_score(actual,predicted)),
        "precision": _clean(precision_score(actual,predicted,zero_division=0)), "recall": _clean(recall_score(actual,predicted,zero_division=0)),
        "f1": _clean(f1_score(actual,predicted,zero_division=0)), "roc_auc": _clean(roc_auc_score(actual,probability)),
        "brier": _clean(brier_score_loss(actual,probability)), "log_loss": _clean(log_loss(actual,probability,labels=[0,1])),
        "recent_50_accuracy": _clean(sum(hits[-50:])/min(50,len(hits))), "recent_100_accuracy": _clean(sum(hits[-100:])/min(100,len(hits))),
        "yearly": {year:{"samples":len(items),"accuracy":_clean(sum(items)/len(items))} for year,items in sorted(yearly.items())},
        "predictions": [{"target_date":d,"actual":a,"prediction":p,"up_probability":_clean(q),"hit":h}
            for d,a,p,q,h in zip(dates,actual,predicted,probability,hits)]}


def _metric_view(result):
    return {key:value for key,value in result.items() if key != "predictions"}


def _regimes(rows, candidate_keys):
    taiex_vol=[]
    changes=[float(r["taiex_change_percent"]) for r in rows]
    for i in range(len(rows)):
        taiex_vol.append(statistics.stdev(changes[max(0,i-19):i+1]) if i >= 19 else None)
    vix_median=statistics.median(float(r["vix_level"]) for r in rows if r.get("vix_level") is not None)
    vol_median=statistics.median(v for v in taiex_vol if v is not None)
    definitions={
        "vix_high": lambda i,r:r.get("vix_level") is not None and r["vix_level"]>=vix_median,
        "vix_low": lambda i,r:r.get("vix_level") is not None and r["vix_level"]<vix_median,
        "taiex_high_volatility": lambda i,r:taiex_vol[i] is not None and taiex_vol[i]>=vol_median,
        "taiex_low_volatility": lambda i,r:taiex_vol[i] is not None and taiex_vol[i]<vol_median,
        "us_market_big_up": lambda i,r:r.get("nasdaq100_change_percent") is not None and r["nasdaq100_change_percent"]>=1,
        "us_market_big_down": lambda i,r:r.get("nasdaq100_change_percent") is not None and r["nasdaq100_change_percent"]<=-1,
        "risk_on": lambda i,r:r.get("nasdaq100_change_percent") is not None and r.get("vix_change_percent") is not None and r["nasdaq100_change_percent"]>0 and r["vix_change_percent"]<0,
        "risk_off": lambda i,r:r.get("nasdaq100_change_percent") is not None and r.get("vix_change_percent") is not None and r["nasdaq100_change_percent"]<0 and r["vix_change_percent"]>0,
    }
    output={}
    for name,test in definitions.items():
        subset=[r for i,r in enumerate(rows) if test(i,r)]
        output[name]={"samples":len(subset),"feature_correlations":{key:_correlation(subset,key,"next_taiex_return")["pearson"] for key in candidate_keys if any(r.get(key) is not None for r in subset)}}
    return output


def _temporal_audit():
    """Independently audit persisted feature timestamps, not just builder memory."""
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT feature_key,count(*) AS checked,
                       count(*) FILTER (WHERE available_at >= cutoff_at) AS failures,
                       min(available_at) AS first_available_at,max(available_at) AS last_available_at
                FROM market_features WHERE transform_version=%s AND target_scope='TAIEX'
                GROUP BY feature_key ORDER BY feature_key
            """, ("v3.13-phase3-1.0",))
            per_feature = [dict(row) for row in cursor.fetchall()]
            cursor.execute("""
                SELECT feature_key,target_date,available_at,cutoff_at,
                       metadata->>'feature_date' AS feature_date
                FROM market_features
                WHERE transform_version=%s AND target_scope='TAIEX' AND available_at >= cutoff_at
                ORDER BY target_date,feature_key LIMIT 100
            """, ("v3.13-phase3-1.0",))
            violations = [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()
    for row in per_feature:
        for key in ("first_available_at", "last_available_at"):
            row[key] = row[key].isoformat() if row[key] else None
    for row in violations:
        row["target_date"] = row["target_date"].isoformat()
        row["available_at"] = row["available_at"].isoformat()
        row["cutoff_at"] = row["cutoff_at"].isoformat()
        row["failure_reason"] = "available_at is not strictly before target open"
    return {"checked_feature_values": sum(row["checked"] for row in per_feature),
        "leakage_count": sum(row["failures"] for row in per_feature), "per_feature": per_feature,
        "suspicious_dates": violations, "rule": "available_at < target_date 09:00 Asia/Taipei",
        "macro_release_note": "ICSA scheduled release timestamps pass cutoff, but historical values are revised FRED rather than ALFRED vintages.",
        "crypto_rule": "Only completed hourly candles whose available_at is before cutoff."}


def analyze():
    rows, formal, globals_ = _load()
    candidates = tuple(key for key in PRIMARY if key in globals_)
    correlations={}
    for key in candidates:
        correlations[key]={"target_direction":_correlation(rows,key,"target_direction"),
            "next_taiex_return":_correlation(rows,key,"next_taiex_return")}
        correlations[key]["target_direction"]["mutual_information"]=_mi(rows,key,"target_direction",True)
        correlations[key]["next_taiex_return"]["mutual_information"]=_mi(rows,key,"next_taiex_return",False)

    common_primary=_complete(rows, (*formal,*candidates))
    if len(common_primary) <= INITIAL: raise ValueError("Insufficient complete cases for walk-forward")
    baseline=_walk(common_primary,formal)
    add_one={}
    for key in candidates:
        result=_walk(common_primary,(*formal,key)); add_one[key]={**_metric_view(result),"delta_vs_baseline":_delta(result,baseline)}
    groups=dict(GROUPS); groups["G_all_global_candidates"]=candidates
    group_results={}
    for name,extras in groups.items():
        result=_walk(common_primary,(*formal,*extras)); group_results[name]={**_metric_view(result),"delta_vs_baseline":_delta(result,baseline)}

    crypto={}
    weekdays=[datetime.fromisoformat(r["target_date"]).weekday() for r in common_primary]
    for key in ("btc_return_1h","btc_return_4h","btc_return_12h","btc_return_24h"):
        model=_walk(common_primary,(*formal,key)); predictions=model["predictions"]
        crypto[key]={"walk_forward":_metric_view(model)}
        for label,condition in (("monday",lambda d:d==0),("tue_to_fri",lambda d:d!=0)):
            selected=[p for p,d in zip(predictions,weekdays[INITIAL:]) if condition(d)]
            crypto[key][label]={"samples":len(selected),"accuracy":_clean(sum(p["hit"] for p in selected)/len(selected)) if selected else None}

    redundancy=[]; redundancy_keys=tuple(dict.fromkeys((*formal,*candidates,*[k for k in globals_ if "return" in k or "zscore" in k])))
    for i,left in enumerate(redundancy_keys):
        for right in redundancy_keys[i+1:]:
            pairs=[(float(r[left]),float(r[right])) for r in rows if r.get(left) is not None and r.get(right) is not None]
            if len(pairs)>=100:
                corr=_pearson([p[0] for p in pairs],[p[1] for p in pairs])
                if abs(corr)>=.85: redundancy.append({"left":left,"right":right,"correlation":_clean(corr),"observations":len(pairs)})

    temporal = _temporal_audit()
    ranking=sorted(((key,add_one[key]["delta_vs_baseline"]["roc_auc"],add_one[key]["delta_vs_baseline"]["accuracy"]) for key in candidates),key=lambda item:(item[1],item[2]),reverse=True)
    report={"generated_at":datetime.now(timezone.utc).isoformat(),"version":"v3.13-phase3-1.0",
        "research_only":True,"dataset":{"rows":len(rows),"complete_case_rows":len(common_primary),"formal_features":len(formal),"global_features":len(globals_),"candidate_features":len(candidates),"oos_predictions":len(common_primary)-INITIAL},
        "correlations_and_mutual_information":correlations,"baseline":_metric_view(baseline),"add_one_in":add_one,
        "group_tests":group_results,"crypto_window_analysis":crypto,"regime_validation":_regimes(common_primary,candidates),
        "highly_correlated_pairs":redundancy,"temporal_leakage_audit":temporal,
        "candidate_ranking_by_roc_auc_then_accuracy":[{"feature":k,"roc_auc_delta":a,"accuracy_delta":b} for k,a,b in ranking],
        "recommended_for_next_round":[k for k,a,b in ranking if a>0 and b>=0][:5],
        "ineffective_or_redundant":[k for k,a,b in ranking if a<=0 and b<=0],
        "limitations":["Analysis is limited to the 722-row TAIEX labelled period (2023-2026).",
            "All experiments are descriptive research; no production feature selection was performed.",
            "ICSA point-in-time revision safety requires ALFRED vintages before production use."]}
    _write(report); return report


def _delta(candidate,baseline):
    keys=("accuracy","precision","recall","f1","roc_auc","brier","log_loss","recent_50_accuracy","recent_100_accuracy")
    return {key:_clean(candidate[key]-baseline[key]) for key in keys}


def _clean(value):
    result=float(value)
    if not math.isfinite(result): raise ValueError("Non-finite analysis value")
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
    try: report=analyze()
    except Exception as exc:
        LOGGER.error("Global feature analysis failed: %s",exc); return 1
    LOGGER.info("Validation complete | candidates=%d | OOS=%d",report["dataset"]["candidate_features"],report["dataset"]["oos_predictions"]); return 0


if __name__=="__main__": raise SystemExit(main())
