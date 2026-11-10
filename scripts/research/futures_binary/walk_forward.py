"""Expanding-window, availability-aware folds for V5 futures research.

This module deliberately returns indices only.  It does not fit a model,
transform features, shuffle rows, calibrate probabilities, or write data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .dataset import BINARY_COLUMN, _aware, prepare_rows


DEFAULT_INITIAL_TRAIN_SIZE = 504
DEFAULT_TEST_SIZE = 21


@dataclass(frozen=True)
class WalkForwardFold:
    """One expanding-window fold expressed in the caller's row indices."""

    fit_at: datetime
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    purged_indices: tuple[int, ...]


def _prepared_with_source_indices(rows: Iterable[Mapping[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Validate raw or previously prepared rows while retaining caller indices."""
    source_rows = list(rows)
    by_date: dict[Any, int] = {}
    raw_rows: list[dict[str, Any]] = []
    for index, source in enumerate(source_rows):
        row = dict(source)
        row.pop(BINARY_COLUMN, None)
        day = row.get("prediction_date")
        if day in by_date:
            raise ValueError(f"Duplicate prediction_date: {day}")
        by_date[day] = index
        raw_rows.append(row)
    prepared = prepare_rows(raw_rows)
    return [(by_date[row["prediction_date"]], row) for row in prepared]


def _eligible_before(rows: list[tuple[int, dict[str, Any]]], fit_at: datetime) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return observable labels and availability-purged prior rows at ``fit_at``."""
    train: list[int] = []
    purged: list[int] = []
    for source_index, row in rows:
        if row["prediction_at"] >= fit_at:
            continue
        if row[BINARY_COLUMN] is not None and row["target_available_at"] <= fit_at:
            train.append(source_index)
        else:
            purged.append(source_index)
    return tuple(train), tuple(purged)


def expanding_walk_forward(
    rows: Iterable[Mapping[str, Any]], *,
    initial_train_size: int = DEFAULT_INITIAL_TRAIN_SIZE,
    test_size: int = DEFAULT_TEST_SIZE,
) -> tuple[WalkForwardFold, ...]:
    """Build availability-safe expanding folds in prediction-session order.

    The first fold begins at the earliest session with at least
    ``initial_train_size`` revealed labels.  Test sessions are never filtered
    on target availability, so the final incomplete block is retained.
    """
    if isinstance(initial_train_size, bool) or not isinstance(initial_train_size, int) or initial_train_size < 1:
        raise ValueError("initial_train_size must be a positive integer")
    if isinstance(test_size, bool) or not isinstance(test_size, int) or test_size < 1:
        raise ValueError("test_size must be a positive integer")
    return expanding_walk_forward_prepared(_prepared_with_source_indices(rows), initial_train_size=initial_train_size,
                                           test_size=test_size)


def expanding_walk_forward_prepared(
    indexed_rows: Iterable[tuple[int, Mapping[str, Any]]], *,
    initial_train_size: int = DEFAULT_INITIAL_TRAIN_SIZE,
    test_size: int = DEFAULT_TEST_SIZE,
    allow_resolved_flat: bool = False,
) -> tuple[WalkForwardFold, ...]:
    """Availability-safe folds for an already validated binary task.

    Unlike :func:`expanding_walk_forward`, this does not impose the V4.6
    next-session target schema.  It is for independently versioned tasks such
    as same-day B4, whose target date equals its prediction date.
    """
    if isinstance(initial_train_size, bool) or not isinstance(initial_train_size, int) or initial_train_size < 1:
        raise ValueError("initial_train_size must be a positive integer")
    if isinstance(test_size, bool) or not isinstance(test_size, int) or test_size < 1:
        raise ValueError("test_size must be a positive integer")
    ordered = [(index, dict(row)) for index, row in indexed_rows]
    seen: set[Any] = set()
    for _, row in ordered:
        day = row.get("prediction_date")
        if day in seen:
            raise ValueError(f"Duplicate prediction_date: {day}")
        seen.add(day)
        _aware(row.get("prediction_at"), "prediction_at")
        available = row.get("target_available_at")
        binary = row.get(BINARY_COLUMN)
        if binary not in {0, 1, None}:
            raise ValueError(f"Invalid {BINARY_COLUMN}")
        resolved_flat = allow_resolved_flat and row.get("direction_class") in {"flat", "FLAT"}
        if binary is None and available is not None and not resolved_flat:
            raise ValueError("Missing binary target cannot have target_available_at")
        if binary is not None or resolved_flat:
            _aware(available, "target_available_at")
    ordered.sort(key=lambda item: item[1]["prediction_at"])
    if not ordered:
        return ()

    start = None
    for position, (_, row) in enumerate(ordered):
        fit_at = _aware(row["prediction_at"], "prediction_at")
        train, _ = _eligible_before(ordered, fit_at)
        if len(train) >= initial_train_size:
            start = position
            break
    if start is None:
        return ()

    folds: list[WalkForwardFold] = []
    for begin in range(start, len(ordered), test_size):
        block = ordered[begin:begin + test_size]
        if not block:
            continue
        fit_at = _aware(block[0][1]["prediction_at"], "prediction_at")
        train, purged = _eligible_before(ordered, fit_at)
        test = tuple(index for index, _ in block)
        if set(train) & set(test) or set(purged) & set(test) or set(train) & set(purged):
            raise AssertionError("walk-forward index sets overlap")
        folds.append(WalkForwardFold(fit_at, train, test, purged))
    return tuple(folds)


def fold_report(folds: Iterable[WalkForwardFold], rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return JSON-ready dry-run metadata without exposing or changing rows."""
    source_rows = list(rows)
    output: list[dict[str, Any]] = []
    for number, fold in enumerate(folds, start=1):
        test_days = [source_rows[index]["prediction_date"] for index in fold.test_indices]
        output.append({
            "fold": number,
            "fit_at": fold.fit_at.isoformat(),
            "train_count": len(fold.train_indices),
            "test_count": len(fold.test_indices),
            "purged_count": len(fold.purged_indices),
            "test_start": min(test_days).isoformat(),
            "test_end": max(test_days).isoformat(),
        })
    return output
