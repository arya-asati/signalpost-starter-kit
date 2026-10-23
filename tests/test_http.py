from __future__ import annotations

import hashlib
import json
import sys
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.http import (  # noqa: E402
    _CountingRedirectHandler,
    fetch_json,
)


class FetchJsonTests(unittest.TestCase):
    def make_response(self, raw: bytes):
        headers = Message()
        headers["Content-Type"] = "application/json; charset=utf-8"

        class FakeResponse:
            status = 200

            def __init__(self):
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return raw

        return FakeResponse()

    def test_success_preserves_raw_response_hash_and_request_count(self):
        raw = (
            '{"organisasjonsnummer":"123456789",'
            '"navn":"Ålesund Example AS"}'
        ).encode("utf-8")
        fake_opener = MagicMock()
        fake_opener.open.return_value = self.make_response(raw)

        with patch(
            "norway_company_agent.http.urllib.request.build_opener",
            return_value=fake_opener,
        ) as mocked_build_opener:
            result = fetch_json("https://example.test/company")

        mocked_build_opener.assert_called_once()
        fake_opener.open.assert_called_once()
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, json.loads(raw))
        self.assertEqual(result.raw_text, raw.decode("utf-8"))
        self.assertEqual(result.content_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.bytes_received, len(raw))
        self.assertEqual(result.request_count, 1)
        self.assertIsNotNone(result.retrieved_at)

    def test_retry_attempts_are_counted(self):
        raw = b'{"ok":true}'
        fake_opener = MagicMock()
        fake_opener.open.side_effect = [
            urllib.error.URLError("temporary network error"),
            self.make_response(raw),
        ]

        with (
            patch(
                "norway_company_agent.http.urllib.request.build_opener",
                return_value=fake_opener,
            ),
            patch("norway_company_agent.http.time.sleep"),
        ):
            result = fetch_json("https://example.test/company", attempts=2)

        self.assertEqual(result.status, 200)
        self.assertEqual(result.request_count, 2)
        self.assertEqual(fake_opener.open.call_count, 2)

    def test_redirect_handler_counts_redirects(self):
        handler = _CountingRedirectHandler()
        request = urllib.request.Request("https://example.test/start")
        headers = Message()

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            headers,
            "https://example.test/next",
        )

        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.full_url, "https://example.test/next")
        self.assertEqual(handler.redirect_count, 1)

    def test_invalid_attempt_count_is_rejected(self):
        with self.assertRaises(ValueError):
            fetch_json("https://example.test/company", attempts=0)


if __name__ == "__main__":
    unittest.main()
