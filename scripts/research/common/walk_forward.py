"""Shared expanding-window boundaries for leakage-safe research."""

from __future__ import annotations

from collections.abc import Iterator


def expanding_window_indices(total_samples: int, initial_training_size: int) -> Iterator[int]:
    """Yield one-step-ahead prediction indices after validating boundaries."""

    if initial_training_size < 2:
        raise ValueError("Initial training size must be at least two")
    if initial_training_size >= total_samples:
        raise ValueError("Initial training size must leave at least one prediction row")
    yield from range(initial_training_size, total_samples)
