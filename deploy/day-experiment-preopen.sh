#!/usr/bin/env bash
# Separate DAY shadow experiment. Never references B4 state or the web API.
set -Eeuo pipefail
: "${DAY_EXPERIMENT_ROOT:?DAY_EXPERIMENT_ROOT is required}"
ROOT="$DAY_EXPERIMENT_ROOT"; RELEASE="$ROOT/current"; PYTHON="$ROOT/venv/bin/python"
DAY="${1:-$(TZ=Asia/Taipei date +%F)}"; MODE="${DAY_EXPERIMENT_MODE:-live}"
DATASET="$ROOT/data_lake/futures_prediction/day_0815_dataset"
STATE="$ROOT/state/day_0815_forward"; SNAPSHOTS="$ROOT/state/day_0815_live_snapshots"
export PYTHONPATH="$RELEASE/scripts" TZ=Asia/Taipei
log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"; }
mkdir -p "$ROOT/state" "$STATE" "$SNAPSHOTS"
exec 9>"$ROOT/state/day-update.lock"
if ! flock -n 9; then log 'SKIP: another DAY experiment job holds the lock'; exit 0; fi
if ! "$PYTHON" - "$DAY" <<'PY'
import sys
from datetime import date
from taifex_trading_calendar import is_taifex_trading_day
raise SystemExit(0 if is_taifex_trading_day(date.fromisoformat(sys.argv[1])) else 1)
PY
then log 'SKIP: not a TAIFEX prediction session'; exit 0; fi
log "START DAY pre-open prediction_date=$DAY"
BUILD="$($PYTHON "$RELEASE/scripts/research/build_futures_day_live_snapshot.py" --prediction-date "$DAY" --output-root "$SNAPSHOTS" --mode "$MODE")" || { log 'FAIL: snapshot builder'; exit 1; }
printf '%s\n' "$BUILD"
SNAPSHOT="$SNAPSHOTS/$DAY.json"
ACTION="$(printf '%s' "$BUILD" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("action", ""))')"
if [[ "$ACTION" == "reused_existing" ]]; then
  STATUS="$(printf '%s' "$BUILD" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
  HASH="$(printf '%s' "$BUILD" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["snapshot_sha256"])')"
  log "SKIP: immutable snapshot already exists; temporal_status=$STATUS sha256=$HASH"
  "$PYTHON" "$RELEASE/scripts/operations/export_futures_day_shadow_json.py" --state-root "$STATE" --output "$ROOT/readonly-api/day-shadow.json"
  exit 0
fi
if ! printf '%s' "$BUILD" | "$PYTHON" -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin)["status"]=="ready" else 1)'; then
  log 'UNAVAILABLE: no substitute or stale prediction will be written'; exit 0
fi
"$PYTHON" "$RELEASE/scripts/research/run_futures_day_forward_prediction.py" --dataset "$DATASET" --snapshot "$SNAPSHOT" --state-root "$STATE" --mode "$MODE"
"$PYTHON" "$RELEASE/scripts/operations/export_futures_day_shadow_json.py" --state-root "$STATE" --output "$ROOT/readonly-api/day-shadow.json"
log 'OK DAY pre-open'
