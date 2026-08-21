#!/usr/bin/env bash
set -euo pipefail

project_dir="/opt/taiwan_stock_analysis_platform"
environment_file="/etc/taiwan-stock-analysis/sync.env"
mode="${1:-daily}"

case "$mode" in
  daily|monthly|quarterly) ;;
  *) echo "Unsupported sync mode: $mode" >&2; exit 2 ;;
esac

cd "$project_dir"
set -a
# shellcheck disable=SC1090
source "$environment_file"
set +a
export PYTHONPATH="$project_dir/scripts"
exec "$project_dir/.venv/bin/python" scripts/sync_stock_database.py "--$mode"
