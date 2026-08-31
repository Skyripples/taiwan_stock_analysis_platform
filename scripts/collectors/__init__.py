"""Shared primitives for resumable, polite official-data collectors."""

from .framework import (
    AllSourcesUnavailable,
    CircuitBreakerOpen,
    CollectorPolicy,
    OfficialHttpClient,
    StructuredLog,
    atomic_json,
    missing_ranges,
)

__all__ = [
    "AllSourcesUnavailable",
    "CircuitBreakerOpen",
    "CollectorPolicy",
    "OfficialHttpClient",
    "StructuredLog",
    "atomic_json",
    "missing_ranges",
]
