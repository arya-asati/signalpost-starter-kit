from __future__ import annotations

import csv
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules  # noqa: E402
from norway_company_agent.output_contract import (  # noqa: E402
    AVAILABILITY_STATES,
    availability_from_evidence,
    contract_envelope,
    validate_contract_envelopes,
)
from scripts.run_competition_batch import (  # noqa: E402
    main as run_batch_main,
    record_worker_failure,
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

    def test_worker_failure_preserves_company_and_creates_failed_evidence(self):
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

        result = record_worker_failure(
            profile,
            ["registry", "website"],
            RuntimeError("simulated worker failure"),
            "2026-10-16T09:01:00Z",
        )

        self.assertEqual(
            result["evidence"]["registry"]["status"],
            "available",
        )
        self.assertEqual(
            result["evidence"]["website"]["status"],
            "source_error",
        )
        self.assertNotIn(
            "source_url",
            result["evidence"]["website"],
        )
        self.assertEqual(
            result["errors"][-1]["kind"],
            "worker_exception",
        )
        self.assertIsNone(
            result["run_metrics"]["requests"]
        )

        envelope = contract_envelope(
            result,
            run_id="worker-failure-test",
            modules=["registry", "website"],
            started_at="2026-10-16T09:00:00Z",
            completed_at="2026-10-16T09:01:00Z",
        )

        validation = validate_contract_envelopes(
            [envelope],
            expected_count=1,
        )
        self.assertTrue(validation["passed"], validation)

        website_claims = [
            claim
            for claim in envelope["claims"]
            if claim["field"] == "official_website"
        ]
        self.assertEqual(len(website_claims), 1)
        self.assertEqual(
            website_claims[0]["availability"],
            "failed",
        )

    def test_batch_runner_writes_outputs_after_worker_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            bulk_path = root / "synthetic-brreg.csv.gz"
            organisations_path = root / "organisations.jsonl"
            output_path = root / "output.jsonl"
            profiles_path = root / "profiles.jsonl"
            report_path = root / "report.json"

            fields = [
                "organisasjonsnummer",
                "navn",
                "organisasjonsform.kode",
                "antallAnsatte",
                "konkurs",
                "underAvvikling",
                "forretningsadresse.kommune",
                "forretningsadresse.kommunenummer",
                "naeringskode1.kode",
                "naeringskode1.beskrivelse",
                "hjemmeside",
                "sisteInnsendteAarsregnskap",
            ]

            row = {
                "organisasjonsnummer": "123456789",
                "navn": "Synthetic Example AS",
                "organisasjonsform.kode": "AS",
                "antallAnsatte": "3",
                "konkurs": "false",
                "underAvvikling": "false",
                "forretningsadresse.kommune": "Oslo",
                "forretningsadresse.kommunenummer": "0301",
                "naeringskode1.kode": "62.010",
                "naeringskode1.beskrivelse": "Computer programming",
                "hjemmeside": "",
                "sisteInnsendteAarsregnskap": "2025",
            }

            with gzip.open(
                bulk_path,
                "wt",
                encoding="utf-8",
                newline="",
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fields,
                    delimiter=";",
                )
                writer.writeheader()
                writer.writerow(row)

            organisations_path.write_text(
                json.dumps({
                    "organisation_number": "123456789"
                }) + "\n",
                encoding="utf-8",
            )

            argv = [
                "run_competition_batch.py",
                "--organisations", str(organisations_path),
                "--bulk", str(bulk_path),
                "--output", str(output_path),
                "--profiles-output", str(profiles_path),
                "--report", str(report_path),
                "--run-id", "simulated-failure",
                "--expected-count", "1",
                "--workers", "1",
                "--checkpoint-every", "1",
                "--modules", "registry,financials",
            ]

            with (
                patch.object(sys, "argv", argv),
                patch(
                    "scripts.run_competition_batch.fetch_official_modules",
                    side_effect=RuntimeError(
                        "synthetic worker failure"
                    ),
                ),
            ):
                with self.assertRaises(SystemExit) as caught:
                    run_batch_main()

            self.assertEqual(caught.exception.code, 1)
            self.assertTrue(output_path.exists())
            self.assertTrue(profiles_path.exists())
            self.assertTrue(report_path.exists())

            output_rows = [
                json.loads(line)
                for line in output_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]

            self.assertEqual(len(output_rows), 1)
            self.assertEqual(
                output_rows[0]["organisation_number"],
                "123456789",
            )

            financial_claims = [
                claim
                for claim in output_rows[0]["claims"]
                if claim["field"] == "financials"
            ]

            self.assertEqual(len(financial_claims), 1)
            self.assertEqual(
                financial_claims[0]["availability"],
                "failed",
            )

            report = json.loads(
                report_path.read_text(encoding="utf-8")
            )

            self.assertEqual(report["emitted_envelopes"], 1)
            self.assertEqual(
                report["operations"]["worker_failures"],
                1,
            )
            self.assertFalse(
                report["operations"]["request_metrics_complete"]
            )

    def test_worker_failure_profile_is_not_complete_for_resume(self):
        profile = {
            "organisation_number": "123456789",
            "name": "Example Company AS",
            "evidence": {
                "registry": {
                    "status": "available",
                },
            },
        }

        failed_profile = record_worker_failure(
            profile,
            ["registry", "financials"],
            RuntimeError("simulated worker failure"),
            "2026-10-16T09:01:00Z",
        )

        self.assertEqual(
            failed_profile["evidence"]["financials"]["status"],
            "source_error",
        )
        self.assertFalse(
            profile_complete_for_modules(
                failed_profile,
                ["registry", "financials"],
            )
        )


if __name__ == "__main__":
    unittest.main()
