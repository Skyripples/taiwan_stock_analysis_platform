"""MOPS market data provider skeleton."""

from __future__ import annotations

from typing import Any

from .base_provider import BaseProvider, NormalizedRecords


class MopsProvider(BaseProvider):
    """Placeholder for a future MOPS integration."""

    name = "MOPS"
    dataset = "mops"
    enabled = False

    def fetch(self) -> Any:
        raise NotImplementedError("MOPS provider has not been configured")

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        raise NotImplementedError("MOPS normalization has not been implemented")

    def validate(self, records: NormalizedRecords) -> bool:
        raise NotImplementedError("MOPS validation has not been implemented")
