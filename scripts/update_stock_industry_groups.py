"""Build the official TPEx Industry Value Chain many-to-many dimension.

The TPEx catalog is fetched by industry-chain page, never one company at a time.
The JSON output is replaced only after every catalog page has parsed successfully.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "data" / "stocks" / "index.json"
OUTPUT_PATH = ROOT / "data" / "stocks" / "industry-groups.json"
BASE_URL = "https://ic.tpex.org.tw"
SOURCE = "TPEx Industry Value Chain"
HEADERS = {"User-Agent": "taiwan-stock-analysis-platform/4.7 (+https://github.com/Skyripples/taiwan_stock_analysis_platform)"}


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", value))).strip()


def group_key(path: str) -> str:
    return "tpex-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:20]


def parse_catalog_codes(document: str) -> list[str]:
    return sorted(set(re.findall(r"introduce\.php\?ic=([A-Za-z0-9]+)", document)))


def parse_catalog(document: str) -> dict[str, str]:
    """Return official chain code -> UTF-8 catalog label.

    TPEx detail headings can contain legacy-encoded text.  Catalog labels are
    UTF-8 and are the authoritative top-level names for taxonomy paths.
    """
    entries: dict[str, str] = {}
    for match in re.finditer(r'<a\s+href="introduce\.php\?ic=([A-Za-z0-9]+)"[^>]*>(.*?)</a>', document, re.S | re.I):
        labels = [clean(value) for value in re.findall(r"<span(?:\s+[^>]*)?>(.*?)</span>", match.group(2), re.S | re.I)]
        if labels:
            entries[match.group(1)] = labels[-1]
    for match in re.finditer(r"<span\s+onclick=\"location\.href='introduce\.php\?ic=([A-Za-z0-9]+)'\"[^>]*>(.*?)</span>", document, re.S | re.I):
        label = re.sub(r"^[^\w\u4e00-\u9fff]+", "", clean(match.group(2))).strip()
        if label:
            entries[match.group(1)] = label
    return entries


def parse_chain_page(document: str, source_url: str, catalog_root: str | None = None) -> list[dict[str, Any]]:
    headings = re.findall(r"<h[1-4][^>]*>(.*?)</h[1-4]>", document, re.S | re.I)
    root = clean(headings[0]).removesuffix("產業鏈簡介") if headings else ""
    if catalog_root:
        root = catalog_root
    if not root:
        raise ValueError("missing official industry-chain heading")
    starts = list(re.finditer(r'<div id="companyList_([A-Za-z0-9]+)" title="([^"]+)"', document))
    if not starts:
        raise ValueError("missing official company lists")
    rows: list[dict[str, Any]] = []
    for index, match in enumerate(starts):
        segment = clean(match.group(2))
        if not segment:
            continue
        block = document[match.end(): starts[index + 1].start() if index + 1 < len(starts) else len(document)]
        path = f"{root} > {segment}"
        for symbol in sorted(set(re.findall(r"company_basic\.php\?stk_code=(\d{4,6})", block))):
            rows.append({"symbol": symbol, "group_key": group_key(path), "group_name": root,
                         "segment_name": segment, "group_path": path, "source": SOURCE,
                         "source_url": source_url, "is_official": True})
    return rows


def universe_symbols() -> set[str]:
    payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return {str(row["symbol"]) for row in payload.get("stocks", [])
            if row.get("active", True) and row.get("instrument_type") == "company"}


def fetch(session: requests.Session, url: str, timeout: float) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def build_snapshot(session: requests.Session, timeout: float, delay: float) -> tuple[list[dict[str, Any]], int]:
    catalog = fetch(session, f"{BASE_URL}/", timeout)
    catalog_entries = parse_catalog(catalog)
    # Fixture/legacy catalog markup may omit labels; retain code discovery for
    # compatibility. Production catalog markup always supplies the labels.
    if not catalog_entries:
        catalog_entries = {code: "TPEx industry chain" for code in parse_catalog_codes(catalog)}
    if not catalog_entries:
        raise RuntimeError("TPEx catalog contains no industry-chain codes")
    universe = universe_symbols()
    associations: dict[tuple[str, str], dict[str, Any]] = {}
    codes = sorted(catalog_entries)
    for number, code in enumerate(codes, 1):
        url = f"{BASE_URL}/introduce.php?ic={code}"
        for row in parse_chain_page(fetch(session, url, timeout), url, catalog_entries[code]):
            if row["symbol"] in universe:
                associations[(row["symbol"], row["group_key"])] = row
        if delay and number < len(codes):
            time.sleep(delay)
    return list(associations.values()), len(codes) + 1


def payload_from(associations: list[dict[str, Any]], requests_made: int) -> dict[str, Any]:
    fetched_at = datetime.now(timezone.utc).isoformat()
    groups: dict[str, dict[str, Any]] = {}
    by_symbol: dict[str, list[str]] = defaultdict(list)
    by_group: dict[str, list[str]] = defaultdict(list)
    for row in associations:
        key = row["group_key"]
        groups[key] = {**{name: row[name] for name in ("group_key", "group_name", "segment_name", "group_path", "source", "source_url", "is_official")}, "active": True}
        by_symbol[row["symbol"]].append(key)
        by_group[key].append(row["symbol"])
    return {"version": "1.0", "updated_at": fetched_at, "source": SOURCE, "complete_snapshot": True,
            "groups": [groups[key] for key in sorted(groups)],
            "by_symbol": {key: sorted(value) for key, value in sorted(by_symbol.items())},
            "by_group": {key: sorted(value) for key, value in sorted(by_group.items())},
            "association_count": len(associations), "request_count": requests_made}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    session = requests.Session(); session.headers.update(HEADERS)
    associations, requests_made = build_snapshot(session, options.timeout, options.delay)
    payload = payload_from(associations, requests_made)
    if not options.dry_run:
        OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"associations": len(associations), "groups": len(payload["groups"]), "requests": requests_made, "dry_run": options.dry_run}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
