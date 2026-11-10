"""Read-only audit for the independent V5 DAY dataset or its source dry-run."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research import build_futures_day_dataset as builder
from research.futures_binary import walk_forward


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=builder.OUTPUT_ROOT)
    parser.add_argument("--source-dry-run", action="store_true", help="Audit source inputs without writing the dataset.")
    parser.add_argument("--tx-root", type=Path, default=builder.TX_ROOT)
    parser.add_argument("--features", type=Path, default=builder.FEATURE_ROOT)
    return parser.parse_args()


def audit(rows: list[dict]) -> dict:
    if len({row["prediction_date"] for row in rows}) != len(rows):
        raise ValueError("duplicate DAY prediction_date")
    rows = sorted(rows, key=lambda row: row["prediction_at"])
    for row in rows:
        if row["prediction_at"].astimezone(builder.TAIPEI).time().isoformat() != "08:15:00":
            raise ValueError(f"DAY cutoff is not 08:15: {row['prediction_date']}")
        if row["feature_pit_status"] == "passed_0815" and row["max_feature_available_at"] is not None:
            if row["max_feature_available_at"] > row["prediction_at"]:
                raise ValueError(f"DAY feature PIT violation: {row['prediction_date']}")
        if row["direction_class"] == "flat" and row[builder.BINARY_COLUMN] is not None:
            raise ValueError(f"FLAT must not enter binary training: {row['prediction_date']}")
    candidates = [(index, row) for index, row in enumerate(rows) if row["feature_pit_status"] == "passed_0815"]
    folds = walk_forward.expanding_walk_forward_prepared(candidates, allow_resolved_flat=True)
    return {
        "task": "DAY", "rows": len(rows),
        "date_range": [rows[0]["prediction_date"].isoformat(), rows[-1]["prediction_date"].isoformat()] if rows else [],
        "labels": dict(sorted(Counter(row["direction_class"] or "missing" for row in rows).items())),
        "feature_pit": dict(sorted(Counter(row["feature_pit_status"] for row in rows).items())),
        "live_readiness": dict(sorted(Counter(row.get("live_readiness", "unknown") for row in rows).items())),
        "selection_evidence": dict(sorted(Counter(row["contract_selection_evidence_status"] for row in rows).items())),
        "binary_training_candidates": sum(row[builder.BINARY_COLUMN] in {0, 1} for _, row in candidates),
        "flat_excluded_from_binary": sum(row["direction_class"] == "flat" for _, row in candidates),
        "fold_count": len(folds), "test_sessions": sum(len(fold.test_indices) for fold in folds),
    }


def read_dataset(root: Path) -> list[dict]:
    import pyarrow.parquet as pq
    rows = []
    for path in sorted(root.rglob("*.parquet")):
        rows.extend(pq.ParquetFile(path).read().to_pylist())
    if not rows:
        raise ValueError("no DAY dataset files")
    return rows


def main() -> int:
    options = arguments()
    try:
        rows = (builder.build_rows(builder.read_tx_daily(options.tx_root), builder.read_daily_features(options.features))
                if options.source_dry_run else read_dataset(options.dataset))
        print(json.dumps(audit(rows), ensure_ascii=False, indent=2, default=str))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
