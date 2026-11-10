"""Create a read-only UI JSON projection from immutable DAY shadow state."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
import sys

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
from research import audit_futures_day_forward_records as audit
from research import run_futures_day_forward_prediction as forward

MODEL_LABELS = {"dummy": "Dummy", "logistic": "Logistic", "hist_gradient_boosting": "HGB"}
MODEL_ORDER = ("dummy", "logistic", "hist_gradient_boosting")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=forward.DEFAULT_STATE_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _metrics(rows: list[dict]) -> dict:
    count = len(rows)
    return {"samples": count, "accuracy": sum(int(row["correct"]) for row in rows) / count if count else None,
            "brier": sum(float(row["brier"]) for row in rows) / count if count else None,
            "log_loss": sum(float(row["log_loss"]) for row in rows) / count if count else None}


def payload(state_root: Path) -> dict:
    predictions = forward._read_jsonl(state_root / "predictions.jsonl")
    evaluations = forward._read_jsonl(state_root / "evaluations.jsonl")
    audit_report = audit.audit(predictions)
    by_key = {(row.get("prediction_date"), row.get("model")): row for row in evaluations}
    exported = [{
        "prediction_date": row.get("prediction_date"), "prediction_at": row.get("prediction_cutoff_at", row.get("cutoff")),
        "generated_at": row.get("generated_at"), "contract": row.get("contract"), "model": row.get("model"),
        "label": MODEL_LABELS.get(row.get("model"), row.get("model")), "model_version": row.get("version"),
        "probability_up": row.get("probability_up"), "direction": row.get("direction"),
        "status": audit.temporal_status(row)[0], "reason": audit.temporal_status(row)[1] or row.get("unavailable_reason"),
        "baseline": row.get("model") == "dummy", "actual_direction": "UP" if evaluation and evaluation.get("label") == 1 else "DOWN" if evaluation else None,
        "hit": bool(evaluation["correct"]) if evaluation else None,
    } for row in predictions for evaluation in [by_key.get((row.get("prediction_date"), row.get("model")))]]
    exported.sort(key=lambda item: (str(item.get("prediction_date")), MODEL_ORDER.index(item["model"]) if item.get("model") in MODEL_ORDER else 99))
    scored = [row for row in evaluations if row.get("prediction_status") in {"early", "on_time", "timely"}]
    dates = sorted({str(row["prediction_date"]) for row in scored})
    return {"preview": True, "data_mode": "real", "read_only": True, "version": forward.VERSION,
            "synced_at": datetime.now().astimezone().isoformat(),
            "source": {"kind": "linode_independent_day_experiment", "path": "readonly-api/day-shadow.json"},
            "dataset": {"name": "DAY 08:15 shadow prediction", "version": forward.VERSION,
                        "target": "08:15 前以 TX Core 預測同日日盤方向；平盤不納入二元評分。", "notice": forward.EXPERIMENTAL_NOTICE},
            "predictions": exported,
            "forward_performance": {"status": "ready" if scored else "pending_close", "timely_only": False,
                                    "message": "僅 early／on_time（及 legacy timely）可納入前瞻績效；late、replay、unavailable 排除。",
                                    "period": {"start": dates[0], "end": dates[-1]} if dates else None,
                                    "models": [{"model": model, "label": MODEL_LABELS[model], "baseline": model == "dummy", **_metrics([row for row in scored if row.get("model") == model])} for model in MODEL_ORDER]},
            "historical_backtest": {"status": "not_synced", "message": "歷史 DAY 回測未隨此唯讀 state 匯出。"},
            "temporal_audit": audit_report, "raw_record_count": len(predictions), "evaluation_count": len(evaluations)}


def write_atomic(value: dict, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".day-shadow-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            json.dump(value, target, ensure_ascii=False, sort_keys=True, indent=2)
            target.write("\n")
            target.flush(); os.fsync(target.fileno())
        os.replace(temporary_name, destination)
        # The deployment directory is setgid to the Nginx reader group.  The
        # derived JSON remains the only group-readable DAY artifact.
        os.chmod(destination, 0o640)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


if __name__ == "__main__":
    options = arguments(); result = payload(options.state_root)
    if not options.dry_run:
        write_atomic(result, options.output)
    print(json.dumps({"output": str(options.output), "dry_run": options.dry_run,
                      "prediction_records": result["raw_record_count"], "evaluations": result["evaluation_count"]}, ensure_ascii=False))
