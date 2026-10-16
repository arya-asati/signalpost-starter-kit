from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.output_contract import (
    AVAILABILITY_STATES,
    availability_from_evidence,
    contract_envelope,
    validate_contract_envelopes,
)


class OutputContractTests(unittest.TestCase):
    def test_all_six_allowed_states_are_preserved(self):
        for state in sorted(AVAILABILITY_STATES):
            with self.subTest(state=state):
                self.assertEqual(
                    availability_from_evidence(
                        state,
                        {"checked": True},
                    ),
                    state,
                )

    def test_internal_statuses_map_to_contract_states(self):
        expected = {
            "not_found": "not_available",
            "not_applicable": "not_applicable",
            "blocked": "blocked",
            "source_error": "failed",
            "not_fetched": "failed",
        }

        for internal, contract in expected.items():
            with self.subTest(internal=internal):
                self.assertEqual(
                    availability_from_evidence(internal),
                    contract,
                )

    def test_missing_value_is_not_converted_to_zero(self):
        self.assertEqual(
            availability_from_evidence("available", None),
            "not_available",
        )
        self.assertEqual(
            availability_from_evidence("available", ""),
            "not_available",
        )

        self.assertEqual(
            availability_from_evidence("available", 0),
            "available",
        )
        self.assertEqual(
            availability_from_evidence("available", False),
            "available",
        )

    def test_checked_empty_list_remains_a_checked_result(self):
        self.assertEqual(
            availability_from_evidence("available", []),
            "available",
        )

    def test_envelope_preserves_identity_and_evidence_references(self):
        profile = {
            "organisation_number": "123456789",
            "name": "Example Company AS",
            "legal_form": "AS",
            "website": "https://example.no/",
            "evidence": {
                "registry": {
                    "field": "registry",
                    "status": "available",
                    "source_type": "official_registry_bulk",
                    "source_class": "official_registry_bulk",
                    "source_url": "https://registry.example/123456789",
                    "retrieved_at": "2026-10-16T09:00:00Z",
                    "content_sha256": "a" * 64,
                    "value": {
                        "organisation_number": "123456789",
                        "name": "Example Company AS",
                    },
                },
                "financials": {
                    "field": "financials",
                    "status": "available",
                    "source_type": "official_annual_accounts",
                    "source_class": "official_annual_accounts",
                    "source_url": "https://accounts.example/123456789",
                    "retrieved_at": "2026-10-16T09:01:00Z",
                    "content_sha256": "b" * 64,
                    "value": {
                        "records": [
                            {"period": "2025", "revenue": 0}
                        ]
                    },
                },
                "website": {
                    "field": "website",
                    "status": "available",
                    "source_type": "registry_linked_company_website",
                    "source_class": "registry_linked_company_website",
                    "source_url": "https://example.no/",
                    "retrieved_at": "2026-10-16T09:02:00Z",
                    "content_sha256": "c" * 64,
                    "value": {
                        "final_url": "https://example.no/",
                        "identity_assessment": {
                            "publishable": True
                        },
                        "title": "Example Company",
                        "description": "Company website",
                        "social_links": [],
                    },
                },
            },
            "run_metrics": {"requests": 3},
        }

        envelope = contract_envelope(
            profile,
            run_id="test-001",
            modules=["registry", "financials", "website"],
            started_at="2026-10-16T09:00:00Z",
            completed_at="2026-10-16T09:03:00Z",
        )

        self.assertEqual(
            envelope["organisation_number"],
            "123456789",
        )
        self.assertEqual(
            envelope["legal_identity"]["organisation_number"],
            "123456789",
        )

        for key in (
            "claims",
            "evidence",
            "changes",
            "errors",
            "operations",
        ):
            self.assertIn(key, envelope)

        evidence_ids = {
            item["id"] for item in envelope["evidence"]
        }
        self.assertTrue(evidence_ids)

        for claim in envelope["claims"]:
            with self.subTest(field=claim["field"]):
                self.assertIn(
                    claim["availability"],
                    AVAILABILITY_STATES,
                )
                self.assertTrue(claim["evidence_ids"])
                self.assertTrue(
                    set(claim["evidence_ids"]).issubset(evidence_ids)
                )

    def test_uncertain_website_is_not_published_as_verified(self):
        profile = {
            "organisation_number": "123456789",
            "name": "Example Company AS",
            "evidence": {
                "website": {
                    "field": "website",
                    "status": "available",
                    "source_type": "registry_linked_company_website",
                    "source_class": "registry_linked_company_website",
                    "source_url": "https://unverified.example/",
                    "retrieved_at": "2026-10-16T09:00:00Z",
                    "content_sha256": "d" * 64,
                    "value": {
                        "final_url": "https://unverified.example/",
                        "title": "Different Company",
                    },
                }
            },
        }

        envelope = contract_envelope(
            profile,
            run_id="test-002",
            modules=["website"],
            started_at="2026-10-16T09:00:00Z",
            completed_at="2026-10-16T09:01:00Z",
        )

        website_claims = [
            claim
            for claim in envelope["claims"]
            if claim["field"].startswith("website")
            or claim["field"] == "official_website"
        ]

        self.assertEqual(len(website_claims), 1)
        self.assertEqual(
            website_claims[0]["availability"],
            "ambiguous",
        )
        self.assertNotEqual(
            website_claims[0]["availability"],
            "available",
        )

    def test_missing_requested_module_has_evidence_reference(self):
        profile = {
            "organisation_number": "123456789",
            "name": "Example Company AS",
            "evidence": {},
        }

        envelope = contract_envelope(
            profile,
            run_id="test-missing-module",
            modules=["registry"],
            started_at="2026-10-16T09:00:00Z",
            completed_at="2026-10-16T09:01:00Z",
        )

        self.assertEqual(len(envelope["claims"]), 1)
        claim = envelope["claims"][0]

        self.assertEqual(claim["availability"], "failed")
        self.assertTrue(claim["evidence_ids"])

        evidence_ids = {
            item["id"] for item in envelope["evidence"]
        }
        self.assertTrue(
            set(claim["evidence_ids"]).issubset(evidence_ids)
        )

    def test_contract_validator_accepts_valid_envelope(self):
        profile = {
            "organisation_number": "123456789",
            "name": "Example Company AS",
            "evidence": {
                "registry": {
                    "field": "registry",
                    "status": "available",
                    "source_type": "official_registry_bulk",
                    "source_class": "official_registry_bulk",
                    "source_url": "https://registry.example/123456789",
                    "retrieved_at": "2026-10-16T09:00:00Z",
                    "content_sha256": "a" * 64,
                    "value": {
                        "organisation_number": "123456789",
                        "name": "Example Company AS",
                    },
                }
            },
        }

        envelope = contract_envelope(
            profile,
            run_id="validator-test",
            modules=["registry"],
            started_at="2026-10-16T09:00:00Z",
            completed_at="2026-10-16T09:01:00Z",
        )

        result = validate_contract_envelopes(
            [envelope],
            expected_count=1,
        )

        self.assertTrue(result["passed"], result)

    def test_contract_validator_rejects_invalid_state_and_dangling_ref(self):
        profile = {
            "organisation_number": "123456789",
            "name": "Example Company AS",
            "evidence": {
                "registry": {
                    "field": "registry",
                    "status": "available",
                    "source_type": "official_registry_bulk",
                    "source_class": "official_registry_bulk",
                    "source_url": "https://registry.example/123456789",
                    "retrieved_at": "2026-10-16T09:00:00Z",
                    "content_sha256": "a" * 64,
                    "value": {
                        "organisation_number": "123456789",
                        "name": "Example Company AS",
                    },
                }
            },
        }

        envelope = contract_envelope(
            profile,
            run_id="validator-test",
            modules=["registry"],
            started_at="2026-10-16T09:00:00Z",
            completed_at="2026-10-16T09:01:00Z",
        )

        broken = dict(envelope)
        broken["claims"] = [
            dict(envelope["claims"][0])
        ]
        broken["claims"][0]["availability"] = "unknown_state"
        broken["claims"][0]["evidence_ids"] = [
            "ev-does-not-exist"
        ]

        result = validate_contract_envelopes(
            [broken],
            expected_count=1,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(
            result["checks"]["all_claim_states_allowed"]
        )
        self.assertFalse(
            result["checks"]["all_claim_evidence_references_resolve"]
        )


if __name__ == "__main__":
    unittest.main()