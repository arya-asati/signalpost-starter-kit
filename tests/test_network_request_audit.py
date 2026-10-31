
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.website import _registered_domain  # noqa: E402


class OfflineDomainExtractionTests(unittest.TestCase):

    def test_domain_extraction_does_not_make_http_requests(self):
        with patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError(
                "Unexpected external HTTP request during domain extraction"
            ),
        ):
            self.assertEqual(
                _registered_domain("https://shop.example.co.uk/products"),
                "example.co.uk",
            )
            self.assertEqual(
                _registered_domain("https://www.example.no/"),
                "example.no",
            )


if __name__ == "__main__":
    unittest.main()
