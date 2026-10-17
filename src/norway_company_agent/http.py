
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_raw(raw: bytes, headers: Any) -> str:
    """Decode captured response bytes without discarding the source text."""
    try:
        charset = (
            headers.get_content_charset()
            if headers is not None
            else None
        )
        return raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, AttributeError):
        return raw.decode("utf-8", errors="replace")


def fetch_json(
    url: str,
    *,
    timeout: float = 20.0,
    attempts: int = 3,
) -> FetchResult:
    last_error = "request failed"

    for attempt in range(attempts):
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
                ),
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:
                raw = response.read()
                elapsed = int(
                    (time.monotonic() - started) * 1000
                )
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
                )

        except urllib.error.HTTPError as exc:
            elapsed = int(
                (time.monotonic() - started) * 1000
            )
            raw = exc.read()

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
                )

            last_error = f"HTTP {exc.code}"

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            last_error = type(exc).__name__

        if attempt + 1 < attempts:
            time.sleep(0.4 * (2**attempt))

    return FetchResult(
        url=url,
        status=0,
        elapsed_ms=0,
        bytes_received=0,
        error=last_error,
        retrieved_at=_utc_now(),
    )
