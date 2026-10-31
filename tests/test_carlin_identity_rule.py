
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.identity import assess_website_identity  # noqa: E402


class CarlinIdentityRuleTests(unittest.TestCase):

    def make_profile(self):
        return {
            "organisation_number": "988573949",
            "name": "CARLIN INVEST AS",
            "website": "www.carlin.no",
            "evidence": {
                "registry": {
                    "status": "available",
                    "value": {
                        "hjemmeside": "www.carlin.no",
                    },
                },
                "roles": {
                    "status": "available",
                    "value": {
                        "roles": [
                            {
                                "name": "Mats Stefan Carlin",
                                "role": "Daglig leder",
                                "role_code": "DAGL",
                                "inactive": False,
                            },
                            {
                                "name": "Mats Stefan Carlin",
                                "role": "Styrets leder",
                                "role_code": "STYR",
                                "inactive": False,
                            },
                        ],
                    },
                },
                "website": {
                    "status": "available",
                    "source_url": "https://www.carlin.no/",
                    "value": {
                        "requested_url": "https://www.carlin.no/",
                        "final_url": "https://www.carlin.no/",
                        "title": "Mats Carlin",
                        "description": "",
                        "main_text_excerpt": (
                            "Jeg heter Mats Carlin. "
                            "I tillegg driver jeg mitt eget "
                            "aksjeselskap Carlin\n"
                            "Invest AS og er styremedlem "
                            "i selskapene Sinvent AS, "
                            "OptoSense AS og GasSecure AS."
                        ),
                    },
                },
            },
        }

    def test_registry_listed_company_with_active_officer_and_explicit_statement_passes(self):
        profile = self.make_profile()

        result = assess_website_identity(profile)

        self.assertTrue(result["publishable"])
        self.assertEqual(result["status"], "exact")
        self.assertEqual(result["score"], 0.93)

        self.assertTrue(
            any(
                "active company officer" in reason
                for reason in result["reasons"]
            )
        )

    def test_company_name_mention_without_ownership_statement_does_not_pass(self):
        profile = self.make_profile()

        profile["evidence"]["website"]["value"]["main_text_excerpt"] = (
            "Mats Carlin works in research and development. "
            "Carlin Invest AS is mentioned in this biography."
        )

        result = assess_website_identity(profile)

        self.assertFalse(result["publishable"])
        self.assertNotEqual(result["score"], 0.93)

    def test_inactive_officer_cannot_verify_website(self):
        profile = self.make_profile()

        for role in profile["evidence"]["roles"]["value"]["roles"]:
            role["inactive"] = True

        result = assess_website_identity(profile)

        self.assertFalse(result["publishable"])
        self.assertNotEqual(result["score"], 0.93)

    def test_different_final_website_domain_is_rejected(self):
        profile = self.make_profile()

        profile["evidence"]["website"]["value"]["final_url"] = (
            "https://another-company.no/"
        )

        result = assess_website_identity(profile)

        self.assertFalse(result["publishable"])
        self.assertNotEqual(result["score"], 0.93)

    def test_group_branded_homepage_cannot_use_special_rule(self):
        profile = self.make_profile()

        profile["evidence"]["website"]["value"]["title"] = (
            "Carlin Group"
        )

        profile["evidence"]["website"]["value"]["description"] = (
            "A group portfolio of companies."
        )

        profile["evidence"]["website"]["value"]["main_text_excerpt"] = (
            "Mats Carlin. "
            "I tillegg driver jeg mitt eget aksjeselskap "
            "Carlin Invest AS. "
            "Carlin Group owns a portfolio of companies."
        )

        result = assess_website_identity(profile)

        self.assertFalse(result["publishable"])
        self.assertNotEqual(result["score"], 0.93)

    def test_missing_official_roles_cannot_verify_website(self):
        profile = self.make_profile()

        profile["evidence"]["roles"]["status"] = "not_found"

        result = assess_website_identity(profile)

        self.assertFalse(result["publishable"])
        self.assertNotEqual(result["score"], 0.93)


if __name__ == "__main__":
    unittest.main()
