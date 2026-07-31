"""Common interface for market analysis components."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict


AnalysisResult = Dict[str, Any]


class BaseAnalysis(ABC):
    """Contract implemented by every market analysis component."""

    analysis_name = "base"
    version = "1.0"

    @abstractmethod
    def load(self) -> Any:
        """Load the source data required by the analysis."""

    @abstractmethod
    def analyze(self, source_data: Any) -> AnalysisResult:
        """Transform source data into analysis results."""

    def export(
        self,
        analysis_result: AnalysisResult,
        *,
        updated_at: str | None = None,
    ) -> AnalysisResult:
        """Build the JSON-ready analysis envelope without writing a file."""

        timestamp = updated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "updated_at": timestamp,
            "version": self.version,
            "signals": analysis_result,
        }
