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


@dataclass(frozen=True)
class UpdateSummary:
    providers: int
    success: int
    failed: int
    skipped: int
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
            LOGGER.error("Provider update failed: %s | %s", provider.name, exc)
            continue
        success += 1

    return UpdateSummary(
        providers=len(provider_list),
        success=success,
        failed=failed,
        skipped=skipped,
        duration_seconds=perf_counter() - started_at,
    )


def log_summary(summary: UpdateSummary) -> None:
    LOGGER.info("Market data update summary")
    LOGGER.info("Providers: %d", summary.providers)
    LOGGER.info("Success: %d", summary.success)
    LOGGER.info("Failed: %d", summary.failed)
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
    return 1 if summary.failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        LOGGER.exception("Unexpected market data update error")
        raise SystemExit(1)
