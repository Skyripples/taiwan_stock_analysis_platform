"""Yahoo market data provider skeleton."""

from __future__ import annotations

from typing import Any

from .base_provider import BaseProvider, NormalizedRecords


class YahooProvider(BaseProvider):
    """Placeholder for a future Yahoo market data integration."""

    name = "Yahoo"
    dataset = "yahoo"
    enabled = False

    def fetch(self) -> Any:
        raise NotImplementedError("Yahoo market provider has not been configured")

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        raise NotImplementedError("Yahoo market normalization has not been implemented")

    def validate(self, records: NormalizedRecords) -> bool:
        raise NotImplementedError("Yahoo market validation has not been implemented")
