"""Small repeatable concurrent smoke benchmark for the deployed REST API."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import psutil
import requests

from config import PROJECT_ROOT


REPORT = PROJECT_ROOT / "data" / "analysis" / "api_performance_report.json"


def percentile(values: list[float], probability: float) -> float:
    return sorted(values)[min(len(values) - 1, math.ceil(len(values) * probability) - 1)]


def request(base: str, path: str) -> tuple[str, float, int]:
    started = time.perf_counter()
    try: status = requests.get(base + path, timeout=10).status_code
    except requests.RequestException: status = 0
    return path, (time.perf_counter() - started) * 1000, status


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--base-url", default="http://127.0.0.1/api/v1")
    parser.add_argument("--concurrency", type=int, default=20); parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--pid", type=int); options = parser.parse_args()
    paths = ["/health", "/stocks?search=2330", "/stocks/2330", "/stocks/2330/financials?limit=12",
             "/industries/%E5%8D%8A%E5%B0%8E%E9%AB%94%E6%A5%AD/peers"]
    jobs = [paths[index % len(paths)] for index in range(options.requests)]
    process = psutil.Process(options.pid) if options.pid else psutil.Process(os.getpid())
    def tree(): return [process] + process.children(recursive=True)
    processes = tree()
    memory_before = sum(item.memory_info().rss for item in processes)
    cpu_before = sum(item.cpu_times().user + item.cpu_times().system for item in processes)
    started = time.perf_counter(); results = []
    with ThreadPoolExecutor(max_workers=options.concurrency) as executor:
        futures = [executor.submit(request, options.base_url, path) for path in jobs]
        for future in as_completed(futures): results.append(future.result())
    elapsed = time.perf_counter() - started; processes = tree()
    memory_after = sum(item.memory_info().rss for item in processes)
    cpu_after = sum(item.cpu_times().user + item.cpu_times().system for item in processes)
    latency = [row[1] for row in results]; errors = [row for row in results if row[2] != 200]
    by_endpoint = {}
    for path in paths:
        values = [row[1] for row in results if row[0] == path]
        by_endpoint[path] = {"requests": len(values), "p50_ms": round(statistics.median(values), 3),
                             "p95_ms": round(percentile(values, .95), 3), "max_ms": round(max(values), 3)}
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "base_url": options.base_url,
              "concurrency": options.concurrency, "requests": len(results), "duration_seconds": round(elapsed, 3),
              "requests_per_second": round(len(results) / elapsed, 2), "p50_ms": round(statistics.median(latency), 3),
              "p95_ms": round(percentile(latency, .95), 3), "max_ms": round(max(latency), 3),
              "error_count": len(errors), "error_rate": round(len(errors) / len(results), 6),
              "process_ram_before_bytes": memory_before, "process_ram_after_bytes": memory_after,
              "process_cpu_percent": round(max(0, cpu_after - cpu_before) / elapsed * 100, 2),
              "process_count": len(processes), "endpoints": by_endpoint}
    temporary = REPORT.with_suffix(".json.tmp"); temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8"); temporary.replace(REPORT)
    print(json.dumps(report)); return 0 if not errors else 1


if __name__ == "__main__": raise SystemExit(main())
