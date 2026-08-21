"""Validate that automated commits are limited to the fallback stock set."""

from __future__ import annotations

import gzip
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from config import PROJECT_ROOT


CONFIG = PROJECT_ROOT / "config" / "fallback_stocks.json"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "update-events.yml"
REPORT = PROJECT_ROOT / "data" / "analysis" / "stock_commit_scope_report.json"
COMMON = (
    "data/stocks/index.json",
    "data/stocks/build_stats.json",
    "data/stocks/industry_snapshot.json",
    "data/stocks/peer_rankings.json",
)


def git(*arguments: str) -> list[str]:
    output = subprocess.check_output(("git", *arguments), cwd=PROJECT_ROOT, text=True, encoding="utf-8")
    return [line for line in output.splitlines() if line]


def main() -> int:
    symbols = [str(item) for item in json.loads(CONFIG.read_text(encoding="utf-8"))["symbols"]]
    errors: list[str] = []
    if not symbols or len(symbols) > 20 or len(symbols) != len(set(symbols)):
        errors.append("fallback allowlist must contain 1-20 unique symbols")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    forbidden = ("git add data/stocks/*.json", "git add data/stocks/history/*.json", "git add data/stocks/financials/*.json")
    errors.extend(f"forbidden staging glob remains: {item}" for item in forbidden if item in workflow)
    if "config/fallback_stocks.json" not in workflow:
        errors.append("workflow does not stage from fallback allowlist")

    allowed = set(COMMON)
    fallback_artifacts: list[Path] = []
    for symbol in symbols:
        for relative in (f"data/stocks/{symbol}.json", f"data/stocks/financials/{symbol}.json", f"data/stocks/history/{symbol}_valuation.json"):
            path = PROJECT_ROOT / relative
            if path.exists():
                allowed.add(relative)
                fallback_artifacts.append(path)
        if not (PROJECT_ROOT / f"data/stocks/{symbol}.json").exists():
            errors.append(f"fallback stock JSON is missing: {symbol}")

    staged = git("diff", "--cached", "--name-only")
    unexpected_stocks = [path for path in staged if path.startswith("data/stocks/") and path not in allowed]
    errors.extend(f"unexpected staged stock artifact: {path}" for path in unexpected_stocks)
    daily_paths = [PROJECT_ROOT / item for item in COMMON] + [PROJECT_ROOT / f"data/stocks/{symbol}.json" for symbol in symbols]
    daily_paths = [path for path in daily_paths if path.exists()]
    raw_bytes = sum(path.stat().st_size for path in daily_paths)
    gzip_bytes = sum(len(gzip.compress(path.read_bytes(), compresslevel=9)) for path in daily_paths)
    tracked_stocks = git("ls-files", "data/stocks")
    git_bytes = sum(path.stat().st_size for path in (PROJECT_ROOT / ".git").rglob("*") if path.is_file())
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "passed" if not errors else "failed",
        "fallback_symbols": symbols,
        "fallback_count": len(symbols),
        "daily_individual_stock_json_count": len(symbols),
        "existing_fallback_artifact_count": len(fallback_artifacts),
        "daily_candidate_raw_bytes": raw_bytes,
        "daily_candidate_independent_gzip_bytes": gzip_bytes,
        "estimated_annual_gzip_upper_bound_bytes": gzip_bytes * 250,
        "tracked_stock_artifact_count": len(tracked_stocks),
        "git_directory_bytes": git_bytes,
        "staged_paths": staged,
        "unexpected_staged_stock_paths": unexpected_stocks,
        "history_rewrite_performed": False,
        "errors": errors,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(REPORT)
    print(json.dumps({key: report[key] for key in ("status", "fallback_count", "daily_individual_stock_json_count", "unexpected_staged_stock_paths")}, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
