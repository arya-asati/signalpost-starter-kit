
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.http import fetch_json  # noqa: E402


class FetchJsonTests(unittest.TestCase):
    def test_success_preserves_raw_response_and_hash(self):
        raw = (
            '{"organisasjonsnummer":"123456789",'
            '"navn":"Ålesund Example AS"}'
        ).encode("utf-8")

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

        with patch(
            "norway_company_agent.http.urllib.request.urlopen",
            return_value=FakeResponse(),
        ) as mocked_urlopen:
            result = fetch_json("https://example.test/company")

        mocked_urlopen.assert_called_once()

        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, json.loads(raw))
        self.assertEqual(result.raw_text, raw.decode("utf-8"))
        self.assertEqual(
            result.content_sha256,
            hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(result.bytes_received, len(raw))
        self.assertIsNotNone(result.retrieved_at)


if __name__ == "__main__":
    unittest.main()
