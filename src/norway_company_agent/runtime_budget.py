from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator


class BudgetExceeded(RuntimeError):
    def __init__(self, dimension: str):
        self.dimension = dimension
        super().__init__(f"{dimension} budget exhausted")


_ACTIVE_BUDGET = threading.local()


@contextmanager
def use_run_budget(budget: "RunBudget") -> Iterator[None]:
    previous = getattr(_ACTIVE_BUDGET, "budget", None)
    _ACTIVE_BUDGET.budget = budget
    try:
        yield
    finally:
        if previous is None:
            try:
                del _ACTIVE_BUDGET.budget
            except AttributeError:
                pass
        else:
            _ACTIVE_BUDGET.budget = previous


def current_run_budget() -> "RunBudget | None":
    return getattr(_ACTIVE_BUDGET, "budget", None)


class RunBudget:
    """Thread-safe run-level limits. Unspecified limits remain unbounded."""

    def __init__(
        self,
        *,
        max_requests: int | None = None,
        max_bytes: int | None = None,
        max_runtime_seconds: float | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.max_runtime_seconds = max_runtime_seconds
        self._clock = clock
        self.started = clock()
        self.requests = 0
        self.bytes_received = 0
        self._reserved_bytes = 0
        self._exhausted_by: str | None = None
        self._thread_requests: dict[int, int] = {}
        self._thread_bytes: dict[int, int] = {}
        self._lock = threading.Lock()

    def _mark_exhausted(self, dimension: str) -> BudgetExceeded:
        if self._exhausted_by is None:
            self._exhausted_by = dimension
        return BudgetExceeded(dimension)

    def observe_time(self) -> bool:
        """Record deadline exhaustion without interrupting required output work."""
        with self._lock:
            if (
                self.max_runtime_seconds is not None
                and self._clock() - self.started >= self.max_runtime_seconds
            ):
                self._mark_exhausted("time")
            return self._exhausted_by == "time"

    def check_time(self) -> None:
        self.observe_time()
        with self._lock:
            if self._exhausted_by is not None:
                raise BudgetExceeded(self._exhausted_by)

    def before_request(self) -> None:
        with self._lock:
            if (
                self.max_runtime_seconds is not None
                and self._clock() - self.started >= self.max_runtime_seconds
            ):
                raise self._mark_exhausted("time")
            if self._exhausted_by is not None:
                raise BudgetExceeded(self._exhausted_by)
            if (
                self.max_requests is not None
                and self.requests >= self.max_requests
            ):
                raise self._mark_exhausted("requests")
            self.requests += 1
            thread_id = threading.get_ident()
            self._thread_requests[thread_id] = (
                self._thread_requests.get(thread_id, 0) + 1
            )

    def request_timeout(self, fallback: float) -> float:
        self.check_time()
        if self.max_runtime_seconds is None:
            return fallback
        remaining = self.max_runtime_seconds - (self._clock() - self.started)
        if remaining <= 0:
            raise self._mark_exhausted("time")
        return min(fallback, remaining)

    def sleep(self, seconds: float) -> None:
        self.check_time()
        remaining = seconds
        if self.max_runtime_seconds is not None:
            remaining = min(
                seconds,
                max(
                    0.0,
                    self.max_runtime_seconds - (self._clock() - self.started),
                ),
            )
        if remaining > 0:
            time.sleep(remaining)
        self.check_time()

    def _reserve_bytes(self, requested: int) -> int:
        self.check_time()
        with self._lock:
            if self._exhausted_by is not None:
                raise BudgetExceeded(self._exhausted_by)
            if self.max_bytes is None:
                self._reserved_bytes += requested
                return requested
            remaining = self.max_bytes - self.bytes_received - self._reserved_bytes
            if remaining <= 0:
                raise self._mark_exhausted("bytes")
            reserved = min(requested, remaining)
            self._reserved_bytes += reserved
            return reserved

    def _settle_bytes(self, reserved: int, received: int) -> None:
        with self._lock:
            self._reserved_bytes -= reserved
            self.bytes_received += received
            thread_id = threading.get_ident()
            self._thread_bytes[thread_id] = (
                self._thread_bytes.get(thread_id, 0) + received
            )

    def current_thread_usage(self) -> dict[str, int]:
        thread_id = threading.get_ident()
        with self._lock:
            return {
                "requests": self._thread_requests.get(thread_id, 0),
                "bytes": self._thread_bytes.get(thread_id, 0),
            }

    def read_response(
        self,
        response: Any,
        *,
        local_max_bytes: int | None = None,
    ) -> bytes:
        """Read a response without crossing the run byte ceiling.

        The local limit reads one extra byte so existing source-specific size
        checks can classify an oversized page. The run-wide limit is never
        exceeded. When a response has no Content-Length and consumes the final
        allowed byte, the read is treated as exhausted because EOF cannot be
        established without receiving another byte.
        """
        if (
            self.max_bytes is None
            and self.max_runtime_seconds is None
            and local_max_bytes is None
        ):
            return response.read()

        local_target = (
            local_max_bytes + 1
            if local_max_bytes is not None
            else None
        )
        try:
            content_length_raw = response.headers.get("Content-Length")
            content_length = (
                int(content_length_raw)
                if content_length_raw is not None
                else None
            )
        except (AttributeError, TypeError, ValueError):
            content_length = None

        if self.max_bytes is not None and content_length is not None:
            with self._lock:
                remaining = (
                    self.max_bytes - self.bytes_received - self._reserved_bytes
                )
            if content_length > remaining:
                raise self._mark_exhausted("bytes")
            if content_length == 0:
                self.check_time()
                return b""

        chunks: list[bytes] = []
        total = 0
        while True:
            self.check_time()
            if local_target is not None and total >= local_target:
                break

            requested = 64 * 1024
            if local_target is not None:
                requested = min(requested, local_target - total)
            reserved = self._reserve_bytes(requested)
            try:
                chunk = response.read(reserved)
            except Exception:
                self._settle_bytes(reserved, 0)
                raise
            self._settle_bytes(reserved, len(chunk))

            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)

            if content_length is not None and total >= content_length:
                break

            if (
                self.max_bytes is not None
                and self.bytes_received >= self.max_bytes
                and (content_length is None or total < content_length)
                and (local_target is None or total < local_target)
            ):
                raise self._mark_exhausted("bytes")

        return b"".join(chunks)

    @property
    def exhausted_by(self) -> str | None:
        try:
            self.check_time()
        except BudgetExceeded:
            pass
        return self._exhausted_by

    @property
    def exhausted(self) -> bool:
        return self.exhausted_by is not None

    def snapshot(self) -> dict[str, Any]:
        self.observe_time()
        elapsed = max(0.0, self._clock() - self.started)
        return {
            "limits": {
                "requests": self.max_requests,
                "bytes": self.max_bytes,
                "runtime_seconds": self.max_runtime_seconds,
            },
            "requests": self.requests,
            "bytes_received": self.bytes_received,
            "elapsed_seconds": round(elapsed, 3),
            "exhausted_by": self.exhausted_by,
        }
