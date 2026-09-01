"""Unified entry point for registered market data providers."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Iterable

from config import MARKET_DATA_DIR
from providers.base_provider import BaseProvider
from providers.registry import registry


LOGGER = logging.getLogger("market_data")

# These datasets supply the 15 features used by the production TAIEX model.
# Failures outside this set are isolated so optional research feeds cannot stop
# the daily signals/history/prediction pipeline.
REQUIRED_DATASETS = frozenset(
    {
        "taiwan_market_overview",
        "institutional_investors",
        "foreign_futures_position",
        "night_futures",
        "tsm_adr",
        "sox_index",
        "sp500_index",
        "nasdaq_index",
        "vix_index",
        "kospi_index",
    }
)


@dataclass(frozen=True)
class UpdateSummary:
    providers: int
    success: int
    failed: int
    skipped: int
    required_failed: int
    optional_failed: int
    duration_seconds: float


def write_json(output_path: Path, payload: object) -> None:
    """Write JSON atomically so an existing valid file survives failures."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
            json.dump(payload, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def execute_provider(provider: BaseProvider) -> Path:
    """Run fetch, normalize, validate, export, and write for one provider."""

    if not provider.dataset or not provider.output_filename:
        raise ValueError(f"Provider {provider.name} is missing dataset output configuration")

    LOGGER.info("Provider update started: %s | dataset=%s", provider.name, provider.dataset)
    raw_data = provider.fetch()
    records = provider.normalize(raw_data)
    if not provider.validate(records):
        raise ValueError(f"Provider {provider.name} produced invalid normalized data")

    payload = provider.export(dataset=provider.dataset, data={"records": records})
    output_path = MARKET_DATA_DIR / provider.output_filename
    write_json(output_path, payload)
    LOGGER.info("Provider update completed: %s | output=%s", provider.name, output_path)
    return output_path


def run_providers(providers: Iterable[BaseProvider]) -> UpdateSummary:
    """Execute enabled providers independently and collect a run summary."""

    started_at = perf_counter()
    provider_list = list(providers)
    success = 0
    failed = 0
    skipped = 0
    required_failed = 0
    optional_failed = 0

    for provider in provider_list:
        if not provider.enabled:
            skipped += 1
            LOGGER.info(
                "Provider skipped: %s | dataset=%s | status=%s",
                provider.name,
                provider.dataset or "not configured",
                provider.status,
            )
            continue

        try:
            execute_provider(provider)
        except Exception as exc:
            failed += 1
            if provider.dataset in REQUIRED_DATASETS:
                required_failed += 1
                failure_kind = "required"
            else:
                optional_failed += 1
                failure_kind = "optional"
            LOGGER.error(
                "Provider update failed: %s | dataset=%s | kind=%s | %s",
                provider.name,
                provider.dataset,
                failure_kind,
                exc,
            )
            continue
        success += 1

    return UpdateSummary(
        providers=len(provider_list),
        success=success,
        failed=failed,
        skipped=skipped,
        required_failed=required_failed,
        optional_failed=optional_failed,
        duration_seconds=perf_counter() - started_at,
    )


def log_summary(summary: UpdateSummary) -> None:
    LOGGER.info("Market data update summary")
    LOGGER.info("Providers: %d", summary.providers)
    LOGGER.info("Success: %d", summary.success)
    LOGGER.info("Failed: %d", summary.failed)
    LOGGER.info("Required failed: %d", summary.required_failed)
    LOGGER.info("Optional failed: %d", summary.optional_failed)
    LOGGER.info("Skipped: %d", summary.skipped)
    LOGGER.info("Duration: %.2f seconds", summary.duration_seconds)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Market data update started")
    LOGGER.info("Output directory: %s", MARKET_DATA_DIR)

    try:
        registry.discover()
        providers = registry.create_providers()
    except Exception as exc:
        LOGGER.error("Provider discovery failed: %s", exc)
        return 1

    LOGGER.info("Provider Registry loaded: %d", registry.count)
    summary = run_providers(providers)
    log_summary(summary)
    if summary.optional_failed:
        LOGGER.warning(
            "Optional provider failures were isolated; successful outputs and the formal prediction pipeline remain usable"
        )
    return 1 if summary.required_failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        LOGGER.exception("Unexpected market data update error")
        raise SystemExit(1)
