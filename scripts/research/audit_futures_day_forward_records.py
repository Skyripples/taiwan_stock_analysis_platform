"""Read-only temporal audit for immutable DAY shadow-prediction records."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from research import run_futures_day_forward_prediction as forward


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=forward.DEFAULT_STATE_ROOT)
    return parser.parse_args()


def temporal_status(record: dict[str, Any]) -> tuple[str, str | None]:
    cutoff = forward._aware(record.get("prediction_cutoff_at", record.get("cutoff")), "prediction cutoff")
    generated = forward._aware(record["generated_at"], "generated_at")
    if record.get("status") in {"unavailable", "replay"}:
        return str(record["status"]), record.get("unavailable_reason")
    if generated < cutoff:
        return "early", "generated_before_prediction_cutoff"
    if generated == cutoff:
        return "on_time", None
    return "late", "generated_after_prediction_cutoff"


def audit(records: list[dict[str, Any]]) -> dict[str, Any]:
    findings = []
    for record in records:
        derived, reason = temporal_status(record)
        findings.append({
            "prediction_date": record.get("prediction_date"), "model": record.get("model"),
            "stored_status": record.get("status"), "derived_temporal_status": derived,
            "generated_at": record.get("generated_at"),
            "prediction_cutoff_at": record.get("prediction_cutoff_at", record.get("cutoff")),
            "reason": reason,
            "legacy_status_mismatch": record.get("status") == "timely" and derived != "on_time",
        })
    return {"read_only": True, "records": len(findings), "findings": findings,
            "note": "Immutable records are not rewritten. Legacy 'timely' means not-late under the prior format; early/on_time/late is derived here."}


def run(options: argparse.Namespace) -> dict[str, Any]:
    return audit(forward._read_jsonl(options.state_root / "predictions.jsonl"))


if __name__ == "__main__":
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
