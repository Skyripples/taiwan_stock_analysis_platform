"""Materialize canonical stock industry groups from the TPEx raw snapshot.

Official paths remain provenance.  The registry/mapping/override JSON files are
the single deterministic source of canonical Layer-2 classification.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/stocks/industry-groups.json"
INDEX = ROOT / "data/stocks/index.json"
REGISTRY = ROOT / "config/stock_industry_group_registry.json"
MAPPING = ROOT / "config/stock_industry_group_mapping.json"
OVERRIDES = ROOT / "config/stock_industry_group_overrides.json"
OUTPUT = ROOT / "data/stocks/stock-groups.json"
AUDIT = ROOT / "reports/v470_industry_group_taxonomy_audit.json"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def map_path(path: str, mapping: dict[str, Any]) -> list[str]:
    root = path.partition(" > ")[0]
    keys = list(mapping.get("root_mappings", {}).get(root, []))
    keys.extend(mapping.get("path_mappings", {}).get(path, []))
    for rule in mapping.get("contains_mappings", []):
        if rule["contains"] in path:
            keys.extend(rule["groups"])
    return sorted(set(keys))


def build() -> tuple[dict[str, Any], dict[str, Any]]:
    raw, index, registry_doc, mapping, overrides_doc = map(load, (RAW, INDEX, REGISTRY, MAPPING, OVERRIDES))
    if not raw.get("complete_snapshot"):
        raise RuntimeError("raw official snapshot is not complete")
    registry = {item["key"]: item for item in registry_doc["groups"] if item.get("active", True)}
    universe = {str(row["symbol"]): row for row in index.get("stocks", []) if row.get("active", True) and row.get("instrument_type") == "company"}
    raw_groups = {item["group_key"]: item for item in raw.get("groups", [])}
    paths_by_symbol: dict[str, list[str]] = defaultdict(list)
    unmapped: set[str] = set()
    canonical: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for symbol, raw_keys in raw.get("by_symbol", {}).items():
        if symbol not in universe:
            continue
        for raw_key in raw_keys:
            item = raw_groups.get(raw_key)
            if not item:
                continue
            path = item["group_path"]
            paths_by_symbol[symbol].append(path)
            keys = map_path(path, mapping)
            if not keys:
                unmapped.add(path)
            for key in keys:
                if key not in registry:
                    raise ValueError(f"mapping references unknown canonical key: {key}")
                row = canonical[symbol].setdefault(key, {"key": key, "name": registry[key]["name"], "provenance": "official-derived", "source_paths": []})
                row["source_paths"].append(path)
    for override in overrides_doc.get("overrides", []):
        symbol, key = str(override["symbol"]), override["group"]
        if symbol not in universe or key not in registry:
            continue
        if not override.get("evidence") or not override.get("source") or not override.get("source_type"):
            raise ValueError(f"curated override lacks evidence: {symbol}/{key}")
        canonical[symbol][key] = {"key": key, "name": registry[key]["name"], "provenance": "curated", "source": override["source"], "source_type": override["source_type"], "evidence": override["evidence"], "verified_at": override["verified_at"], "source_paths": []}
    by_symbol: dict[str, Any] = {}
    by_group: dict[str, list[str]] = defaultdict(list)
    for symbol, item in universe.items():
        groups = []
        for key, association in sorted(canonical.get(symbol, {}).items()):
            association["source_paths"] = sorted(set(association["source_paths"]))
            groups.append(association); by_group[key].append(symbol)
        by_symbol[symbol] = {"industry": item.get("industry"), "groups": groups}
    associations = sum(len(row["groups"]) for row in by_symbol.values())
    paths = sorted(item["group_path"] for item in raw_groups.values())
    sizes = sorted(len(row["groups"]) for row in by_symbol.values())
    root_counts = Counter(path.partition(" > ")[0] for path in paths)
    leaf_counts = Counter(path.partition(" > ")[2] for path in paths)
    audit = {
        "version": "1.0", "generated_at": datetime.now(timezone.utc).isoformat(),
        "official_path_count": len(paths), "top_level_official_chains": [{"name": key, "paths": value} for key, value in sorted(root_counts.items())],
        "leaf_segments": [{"name": key, "paths": value} for key, value in sorted(leaf_counts.items())],
        "repeated_leaf_segments": [{"name": key, "paths": value} for key, value in sorted(leaf_counts.items()) if value > 1],
        "mapping_configuration": {"root_mappings": mapping.get("root_mappings", {}), "path_mappings": mapping.get("path_mappings", {}), "contains_mappings": mapping.get("contains_mappings", [])},
        "company_universe": len(universe), "companies_with_canonical_groups": sum(bool(v["groups"]) for v in by_symbol.values()),
        "companies_with_zero_canonical_groups": sum(not v["groups"] for v in by_symbol.values()), "canonical_group_count": len(registry),
        "total_canonical_associations": associations, "groups_per_company": {"average": associations / len(universe) if universe else 0, "median": sizes[len(sizes)//2] if sizes else 0, "max": max(sizes, default=0)},
        "top_groups": [{"key": key, "name": registry[key]["name"], "symbols": len(symbols)} for key, symbols in sorted(by_group.items(), key=lambda pair: (-len(pair[1]), pair[0]))[:30]],
        "groups_size_one": sorted(key for key, symbols in by_group.items() if len(symbols) == 1), "unmapped_official_paths": sorted(unmapped),
        "duplicate_associations": 0,
        "examples": {symbol: {"industry": by_symbol.get(symbol, {}).get("industry"), "groups": by_symbol.get(symbol, {}).get("groups", []), "official_paths": sorted(set(paths_by_symbol.get(symbol, [])))} for symbol in ("3037", "8046", "3189", "2327", "2382", "3231", "6669", "2308")},
    }
    output = {"version": "1.0", "updated_at": audit["generated_at"], "source": raw.get("source"), "complete_snapshot": True,
              "registry": [{key: item[key] for key in ("key", "name", "aliases", "source", "active")} for item in registry.values()],
              "by_symbol": by_symbol, "by_group": {key: sorted(symbols) for key, symbols in sorted(by_group.items())}, "association_count": associations}
    return output, audit


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--dry-run", action="store_true"); options = parser.parse_args()
    output, audit = build()
    if not options.dry_run:
        OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"companies": audit["company_universe"], "associations": output["association_count"], "unmapped_paths": len(audit["unmapped_official_paths"]), "dry_run": options.dry_run}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
