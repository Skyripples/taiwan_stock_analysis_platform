"""Shared, model-free definitions for the four V5 TX research tasks."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FuturesTaskDefinition:
    key: str
    timezone: str
    cutoff: str
    start: str
    end: str
    label_rule: str
    implementation_status: str


DAY = FuturesTaskDefinition(
    "DAY", "Asia/Taipei", "08:15", "08:45", "regular-session close",
    "same physical TX contract; UP close>open, DOWN close<open, FLAT separate", "implemented",
)
NIGHT = FuturesTaskDefinition(
    "NIGHT", "Asia/Taipei", "14:30", "15:00", "next calendar day 05:00",
    "same physical TX contract; UP close>open, DOWN close<open, FLAT separate", "planned",
)
US_PRE = FuturesTaskDefinition(
    "US_PRE", "America/New_York", "09:00", "09:00", "10:00",
    "same physical TX contract; UP end>start, DOWN end<start, FLAT separate", "labels_only",
)
US_POST = FuturesTaskDefinition(
    "US_POST", "America/New_York", "10:00", "10:00", "11:00",
    "same physical TX contract; UP end>start, DOWN end<start, FLAT separate", "labels_only",
)
TASKS = (DAY, NIGHT, US_PRE, US_POST)
