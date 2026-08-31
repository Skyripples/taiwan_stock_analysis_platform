"""Registry of Taiwan collectors integrated with the shared framework."""

from __future__ import annotations

from collections.abc import Callable

from scripts.research import backfill_taiwan_industry_indices, backfill_taiwan_pit_foundation

COLLECTORS: dict[str, Callable[[list[str] | None], int]] = {
    "taiwan_industry_indices": backfill_taiwan_industry_indices.main,
    "taiwan_pit": backfill_taiwan_pit_foundation.main,
}
