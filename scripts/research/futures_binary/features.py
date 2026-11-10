"""Explicit V5 feature contract; unknown columns are excluded by default.

Group membership is a research candidate list, not a certification of source
vintages or derived-feature provenance. DGS10 stays quarantined. This module
does no imputation, scaling, feature selection fitting, or row filtering.
"""

from __future__ import annotations

import math
from numbers import Real
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .dataset import BINARY_COLUMN, RETURN_COLUMN


FEATURE_GROUPS = MappingProxyType({
    "tx_core": (
        "tx_previous_day_return", "tx_open_to_close_return",
        "tx_intraday_range_pct", "tx_true_range_pct", "tx_return_volatility_5d",
        "tx_volume", "tx_volume_change_1d", "tx_open_interest",
        "tx_open_interest_change_1d", "tx_open_interest_change_pct",
        "tx_days_to_expiry", "tx_expiry_week", "tx_roll_regime",
    ),
    "term_structure": (
        "tx_front_next_spread", "tx_front_next_spread_pct", "tx_basis", "tx_basis_pct",
    ),
    "global": (
        "sox_value", "tsm_adr_value", "nasdaq_value", "sp500_value", "usdtwd_value",
    ),
    "institutional": tuple(
        f"{investor}_{metric}"
        for investor in ("foreign", "investment_trust", "dealer")
        for metric in ("long_oi", "short_oi", "net_oi", "net_oi_change")
    ),
    "pcr": ("put_volume", "call_volume", "volume_pcr", "put_oi", "call_oi", "oi_pcr"),
})
DEFAULT_GROUPS = ("tx_core",)
FEATURE_ALLOWLIST = frozenset(name for group in FEATURE_GROUPS.values() for name in group)
LEAKAGE_BLACKLIST = frozenset((
    RETURN_COLUMN, BINARY_COLUMN, "direction_class", "direction_down_max", "direction_up_min",
))
QUARANTINED_FEATURES = MappingProxyType({
    "dgs10_value": "Historical vintage provenance has not been verified",
})


def exclusion_reason(name: str) -> str | None:
    """None means allowed; all other names receive a reason for exclusion."""
    if name in LEAKAGE_BLACKLIST or name.startswith("target_"):
        return "target or label metadata"
    if name in QUARANTINED_FEATURES:
        return QUARANTINED_FEATURES[name]
    if (name.endswith(("_available_at", "_date", "_version", "_contract"))
            or name in ("prediction_at", "partition_year", "quality_flags")):
        return "metadata; validation and reporting only"
    if name not in FEATURE_ALLOWLIST:
        return "not in the explicit feature allowlist"
    return None


def validate_feature_names(names: Iterable[str]) -> tuple[str, ...]:
    if isinstance(names, str):
        raise ValueError("Feature names must be a sequence, not a string")
    columns = tuple(names)
    if not columns:
        raise ValueError("At least one feature is required")
    if any(not isinstance(name, str) for name in columns):
        raise ValueError("Feature names must be strings")
    if len(set(columns)) != len(columns):
        raise ValueError("Duplicate feature names")
    for name in columns:
        reason = exclusion_reason(name)
        if reason is not None:
            raise ValueError(f"Excluded feature {name}: {reason}")
    return columns


def feature_names(groups: Iterable[str] = DEFAULT_GROUPS) -> tuple[str, ...]:
    """Return ordered columns for explicitly requested groups; TX Core by default."""
    if isinstance(groups, str):
        raise ValueError("Groups must be a sequence, not a string")
    selected = tuple(groups)
    if any(not isinstance(group, str) for group in selected):
        raise ValueError("Group names must be strings")
    if len(set(selected)) != len(selected):
        raise ValueError("Duplicate feature groups")
    for group in selected:
        if group not in FEATURE_GROUPS:
            raise ValueError(f"Unknown feature group: {group}")
    return validate_feature_names(name for group in selected for name in FEATURE_GROUPS[group])


def select_feature_rows(
    rows: Iterable[Mapping[str, Any]], columns: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Project validated dataset rows onto allowed features without losing rows.

Call after dataset.load_dataset/prepare_rows for PIT checks. Missing values
stay None for later train-only preprocessing. An absent column is a schema
error, distinct from a present optional column containing None.
"""
    names = feature_names() if columns is None else validate_feature_names(columns)
    output = []
    for index, row in enumerate(rows):
        projected = {}
        for name in names:
            if name not in row:
                raise ValueError(f"Missing feature column at row {index}: {name}")
            value = row[name]
            if value is not None and (not isinstance(value, Real) or not math.isfinite(value)):
                raise ValueError(f"Feature must be finite numeric or None at row {index}: {name}")
            projected[name] = value
        output.append(projected)
    return output
