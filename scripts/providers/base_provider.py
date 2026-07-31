"""Common interface for market data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List


NormalizedRecord = Dict[str, Any]
NormalizedRecords = List[NormalizedRecord]


class BaseProvider(ABC):
    """Contract implemented by every market data provider.

    Providers are disabled until their real data integration is implemented.
    The update entry point can safely discover disabled providers without
    invoking network or normalization work.
    """

    name = "base"
    dataset = ""
    output_filename = ""
    enabled = True

    @abstractmethod
    def fetch(self) -> Any:
        """Fetch raw data from the provider's source."""

    @abstractmethod
    def normalize(self, raw_data: Any) -> NormalizedRecords:
        """Convert raw provider data into the platform's normalized records."""

    @abstractmethod
    def validate(self, records: NormalizedRecords) -> bool:
        """Return whether normalized records satisfy this provider's rules."""

    def export(
        self,
        *,
        dataset: str,
        data: Any,
        version: str = "1.0",
        updated_at: str | None = None,
    ) -> Dict[str, Any]:
        """Build a JSON-ready market data envelope without writing a file.

        Provider implementations may prepare different shapes inside ``data``,
        while the top-level fields remain consistent across the platform.
        """

        timestamp = updated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "updated_at": timestamp,
            "provider": self.name,
            "dataset": dataset,
            "version": version,
            "data": data,
        }

    @property
    def status(self) -> str:
        """Return a stable status used by the update command's logs."""

        return "enabled" if self.enabled else "not configured"
