"""Reusable collector transport, state, logging, and coverage planning.

The module deliberately knows nothing about a particular official payload. Source
adapters remain responsible for parsing and validation, while this layer owns the
long-running operational behavior.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")


class CircuitBreakerOpen(RuntimeError):
    """Raised after repeated throttling instead of waiting without a bound."""


class AllSourcesUnavailable(RuntimeError):
    """Raised when primary and every configured official mirror are open."""

    def __init__(self, sources: list[str], errors: list[str]) -> None:
        self.sources = sources
        self.errors = errors
        super().__init__("All configured official sources are unavailable: " + " | ".join(errors))


class SourceAdapter(Protocol):
    """Payload-specific contract used by collector entry points."""

    name: str

    def plan(self, start: date, end: date, refresh: bool) -> list[Any]: ...

    def fetch(self, job: Any, client: "OfficialHttpClient") -> list[dict[str, Any]]: ...

    def persist(self, rows: list[dict[str, Any]]) -> None: ...


@dataclass(frozen=True)
class CollectorPolicy:
    request_interval_seconds: float = 1.0
    timeout_connect_seconds: float = 10.0
    timeout_read_seconds: float = 45.0
    retries: int = 4
    backoff_initial_seconds: float = 2.0
    backoff_max_seconds: float = 30.0
    throttle_circuit_threshold: int = 3
    circuit_cooldown_seconds: float = 120.0
    source_failure_circuit_threshold: int = 2

    @classmethod
    def from_config(cls, value: dict[str, Any] | None) -> "CollectorPolicy":
        raw = value or {}
        return cls(**{field: raw[field] for field in cls.__dataclass_fields__ if field in raw})


class StructuredLog:
    """Append-only JSONL log suitable for unattended runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(TAIPEI).isoformat(timespec="milliseconds"),
            "timezone": "Asia/Taipei",
            "event": event,
            **fields,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")


class OfficialHttpClient:
    """Rate-limited HTTP client with adaptive throttling and a circuit breaker."""

    def __init__(
        self,
        policy: CollectorPolicy,
        user_agent: str,
        log: StructuredLog,
        session_factory: Callable[[], requests.Session] | None = None,
    ) -> None:
        self.policy = policy
        self.user_agent = user_agent
        self.log = log
        self._session_factory = session_factory or requests.Session
        self._local = threading.local()
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._adaptive_interval = policy.request_interval_seconds
        self._throttle_streak = 0
        self._circuit_opened_at: float | None = None
        self._source_failures: dict[str, int] = {}
        self._source_circuits: dict[str, float] = {}
        self.request_count = 0

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            session.headers.update({"User-Agent": self.user_agent, "Accept": "application/json"})
            self._local.session = session
        return session

    def request_json(self, method: str, urls: str | list[str], **kwargs: Any) -> Any:
        candidates = [urls] if isinstance(urls, str) else list(urls)
        if not candidates:
            raise ValueError("At least one official source URL is required")
        errors: list[str] = []
        failed_sources: list[str] = []
        for index, url in enumerate(candidates):
            if self._source_is_open(url):
                failed_sources.append(url)
                errors.append(f"source circuit open: {url}")
                continue
            try:
                payload = self._request_one(method, url, **kwargs)
                self._source_failures[url] = 0
                return payload
            except CircuitBreakerOpen as exc:
                errors.append(str(exc))
                failed_sources.append(url)
                self._open_source(url, str(exc))
                if index + 1 >= len(candidates):
                    break
                # The breaker belongs to the throttled origin. An explicitly
                # configured official mirror gets one independent opportunity.
                self.log.emit("source_fallback", source=url, error=str(exc), reason="circuit_open")
                self._circuit_opened_at = None
                self._throttle_streak = 0
                self._adaptive_interval = self.policy.request_interval_seconds
            except RuntimeError as exc:
                errors.append(str(exc))
                failed_sources.append(url)
                self.log.emit("source_fallback", source=url, error=str(exc))
        raise AllSourcesUnavailable(sorted(set(failed_sources)), errors)

    def _source_is_open(self, url: str) -> bool:
        opened = self._source_circuits.get(url)
        if opened is None:
            return False
        if time.monotonic() - opened < self.policy.circuit_cooldown_seconds:
            return True
        del self._source_circuits[url]
        self._source_failures[url] = 0
        self.log.emit("source_circuit_half_open", source=url)
        return False

    def _open_source(self, url: str, reason: str) -> None:
        self._source_circuits[url] = time.monotonic()
        self.log.emit("source_circuit_open", source=url, reason=reason)

    def _request_one(self, method: str, url: str, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.policy.retries + 1):
            self._check_circuit()
            try:
                with self._lock:
                    wait = self._adaptive_interval - (time.monotonic() - self._last_request)
                    if wait > 0:
                        time.sleep(wait)
                    self._last_request = time.monotonic()
                    self.request_count += 1
                started = time.perf_counter()
                response = self.session().request(
                    method,
                    url,
                    timeout=(self.policy.timeout_connect_seconds, self.policy.timeout_read_seconds),
                    **kwargs,
                )
                status = response.status_code
                if status in {403, 429}:
                    self._record_throttle(url, status, attempt)
                    response.raise_for_status()
                response.raise_for_status()
                response.encoding = "utf-8"
                payload = response.json()
                self._throttle_streak = 0
                self._adaptive_interval = max(
                    self.policy.request_interval_seconds,
                    self._adaptive_interval * 0.9,
                )
                self.log.emit(
                    "request_ok", source=url, status=status, attempt=attempt,
                    duration_seconds=round(time.perf_counter() - started, 4),
                )
                return payload
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status not in {403, 429}:
                    failures = self._source_failures.get(url, 0) + 1
                    self._source_failures[url] = failures
                    if failures >= self.policy.source_failure_circuit_threshold:
                        self._open_source(url, str(exc))
                        raise CircuitBreakerOpen(
                            f"Official source circuit opened after {failures} consecutive failures: {url}"
                        ) from exc
                if attempt >= self.policy.retries:
                    break
                delay = min(
                    self.policy.backoff_initial_seconds * (2**attempt),
                    self.policy.backoff_max_seconds,
                )
                self.log.emit("request_retry", source=url, status=status, attempt=attempt + 1, delay_seconds=delay)
                time.sleep(delay + random.random() * min(0.2, delay / 10))
        raise RuntimeError(f"Official request failed: {url}: {last_error}")

    def _record_throttle(self, url: str, status: int, attempt: int) -> None:
        self._throttle_streak += 1
        self._adaptive_interval = min(
            max(self._adaptive_interval * 2, self.policy.request_interval_seconds),
            self.policy.backoff_max_seconds,
        )
        self.log.emit(
            "request_throttled", source=url, status=status, attempt=attempt,
            throttle_streak=self._throttle_streak, next_interval_seconds=self._adaptive_interval,
        )
        if self._throttle_streak >= self.policy.throttle_circuit_threshold:
            self._circuit_opened_at = time.monotonic()
            raise CircuitBreakerOpen(
                f"Official source circuit opened after {self._throttle_streak} responses with status {status}"
            )

    def _check_circuit(self) -> None:
        if self._circuit_opened_at is None:
            return
        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed < self.policy.circuit_cooldown_seconds:
            raise CircuitBreakerOpen(
                f"Official source circuit remains open; retry after "
                f"{self.policy.circuit_cooldown_seconds - elapsed:.1f}s"
            )
        self._circuit_opened_at = None
        self._throttle_streak = 0
        self.log.emit("circuit_half_open")


def missing_ranges(start: date, end: date, covered: list[tuple[date, date]], refresh: bool = False) -> list[tuple[date, date]]:
    """Plan forward/backward gaps without performing I/O or network requests."""
    if start > end:
        raise ValueError("start must not be later than end")
    if refresh:
        return [(start, end)]
    merged: list[tuple[date, date]] = []
    for lower, upper in sorted(covered):
        if lower > upper:
            raise ValueError("invalid coverage range")
        if not merged or lower > merged[-1][1] + timedelta(days=1):
            merged.append((lower, upper))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], upper))
    cursor = start
    gaps: list[tuple[date, date]] = []
    for lower, upper in merged:
        if upper < cursor or lower > end:
            continue
        if cursor < lower:
            gaps.append((cursor, min(end, lower - timedelta(days=1))))
        cursor = max(cursor, upper + timedelta(days=1))
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
