from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .runtime_budget import BudgetExceeded, current_run_budget


class _CountingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Counts redirect requests in addition to each original open attempt."""

    def __init__(self, budget: Any = None) -> None:
        super().__init__()
        self.redirect_count = 0
        self.budget = budget

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        if self.budget is not None:
            self.budget.before_request()
        self.redirect_count += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class FetchResult:
    url: str
    status: int
    elapsed_ms: int
    bytes_received: int
    body: Any = None
    error: str | None = None
    content_sha256: str | None = None
    retrieved_at: str | None = None
    effective_at: str | None = None
    # Appended to preserve compatibility with existing positional callers.
    raw_text: str | None = None
    # Total outbound HTTP request attempts, including retries and redirects.
    request_count: int = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_raw(raw: bytes, headers: Any) -> str:
    """Decode captured response bytes without discarding the source text."""
    try:
        charset = headers.get_content_charset() if headers is not None else None
        return raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, AttributeError):
        return raw.decode("utf-8", errors="replace")


def fetch_json(
    url: str,
    *,
    timeout: float = 20.0,
    attempts: int = 3,
) -> FetchResult:
    """Fetch JSON and account for initial requests, retries, and redirects."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    last_error = "request failed"
    attempts_made = 0
    budget = current_run_budget()
    redirect_handler = _CountingRedirectHandler(budget)
    opener = urllib.request.build_opener(redirect_handler)

    for attempt in range(attempts):
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)",
            },
        )
        if budget is not None:
            budget.before_request()
        attempts_made += 1

        try:
            effective_timeout = (
                budget.request_timeout(timeout)
                if budget is not None
                else timeout
            )
            with opener.open(request, timeout=effective_timeout) as response:
                raw = (
                    budget.read_response(response)
                    if budget is not None
                    else response.read()
                )
                elapsed = int((time.monotonic() - started) * 1000)
                raw_text = _decode_raw(raw, response.headers)
                body = json.loads(raw)
                return FetchResult(
                    url=url,
                    status=response.status,
                    elapsed_ms=elapsed,
                    bytes_received=len(raw),
                    body=body,
                    content_sha256=hashlib.sha256(raw).hexdigest(),
                    retrieved_at=_utc_now(),
                    raw_text=raw_text,
                    request_count=attempts_made + redirect_handler.redirect_count,
                )

        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raw = (
                budget.read_response(exc)
                if budget is not None
                else exc.read()
            )

            # Not-found responses are terminal and should not be retried.
            if exc.code in {404, 410}:
                return FetchResult(
                    url=url,
                    status=exc.code,
                    elapsed_ms=elapsed,
                    bytes_received=len(raw),
                    error=f"HTTP {exc.code}",
                    content_sha256=hashlib.sha256(raw).hexdigest(),
                    retrieved_at=_utc_now(),
                    raw_text=_decode_raw(raw, exc.headers),
                    request_count=attempts_made + redirect_handler.redirect_count,
                )

            last_error = f"HTTP {exc.code}"

        except BudgetExceeded:
            raise

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            last_error = type(exc).__name__

        if attempt + 1 < attempts:
            delay = 0.4 * (2**attempt)
            if budget is not None:
                budget.sleep(delay)
            else:
                time.sleep(delay)

    return FetchResult(
        url=url,
        status=0,
        elapsed_ms=0,
        bytes_received=0,
        error=last_error,
        retrieved_at=_utc_now(),
        request_count=attempts_made + redirect_handler.redirect_count,
    )
