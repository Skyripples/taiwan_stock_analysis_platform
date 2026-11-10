#!/usr/bin/env bash
# Separate DAY post-close flow. Exit 75 means official daily data is pending.
set -Eeuo pipefail
: "${DAY_EXPERIMENT_ROOT:?DAY_EXPERIMENT_ROOT is required}"
ROOT="$DAY_EXPERIMENT_ROOT"; RELEASE="$ROOT/current"; PYTHON="$ROOT/venv/bin/python"
DAY="${1:-$(TZ=Asia/Taipei date +%F)}"; STATE="$ROOT/state/day_0815_forward"
DATA="$ROOT/data_lake"; DATASET="$DATA/futures_prediction/day_0815_dataset"; FEATURES="$DATA/futures_prediction/daily_features"
export PYTHONPATH="$RELEASE/scripts" TZ=Asia/Taipei
log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"; }
pending() { printf '{"event":"PENDING","trade_date":"%s","recorded_at":"%s","reason":"%s"}\n' "$DAY" "$(date --iso-8601=seconds)" "$1" >> "$ROOT/state/postclose-events.jsonl"; log "PENDING: $1"; exit 75; }
mkdir -p "$ROOT/state" "$STATE"; exec 9>"$ROOT/state/day-update.lock"
if ! flock -n 9; then log 'SKIP: another DAY experiment job holds the lock'; exit 0; fi
UPDATE="$($PYTHON "$RELEASE/scripts/update_futures_daily.py" --products TX --staging-only --start "$DAY" --end "$DAY" --missing-only)" || pending 'official_daily_updater_failed'
printf '%s\n' "$UPDATE"
if ! printf '%s' "$UPDATE" | "$PYTHON" -c 'import json,sys; p=json.load(sys.stdin); raise SystemExit(0 if not p["failures"] and not p["skipped_not_yet_published"] else 1)'; then pending 'official_daily_not_published'; fi
"$PYTHON" "$RELEASE/scripts/research/build_futures_daily_pit_alignment.py" --start "$DAY" --end "$DAY" --output "$FEATURES"
"$PYTHON" "$RELEASE/scripts/research/build_futures_day_dataset.py" --features "$FEATURES" --output "$DATASET"
"$PYTHON" "$RELEASE/scripts/research/run_futures_day_forward_prediction.py" --dataset "$DATASET" --state-root "$STATE" --evaluate-only
"$PYTHON" "$RELEASE/scripts/operations/export_futures_day_shadow_json.py" --state-root "$STATE" --output "$ROOT/readonly-api/day-shadow.json"
log 'OK DAY post-close'
