from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# When copied into the project, the source package is at ../src.
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.identity import (  # noqa: E402
    apply_website_identity_gate,
    assess_social_identity,
    assess_website_identity,
)


class WebsiteAuditIdentityTests(unittest.TestCase):
    def test_group_branded_homepage_does_not_pass_from_subsidiary_footer_mention(self):
        profile = {
            "organisation_number": "988412406",
            "name": "Tevlingveien 23 Invest AS",
            "evidence": {"website": {
                "status": "available",
                "source_url": "https://ragde.no/",
                "value": {
                    "final_url": "https://ragde.no/",
                    "title": "Ragde Eiendom",
                    "identity_text_excerpt": "Ragde Eiendom. Tevlingveien 23 Invest AS is one of our companies.",
                    "main_text_excerpt": "Ragde Eiendom owns and manages a portfolio of real estate companies." * 3,
                },
            }},
        }
        assessment = assess_website_identity(profile)
        self.assertFalse(assessment["publishable"])
        self.assertEqual(assessment["status"], "related_or_uncertain")
        self.assertTrue(any("parent brand" in reason or "conflicting brand" in reason for reason in assessment["reasons"]))

    def test_generic_title_can_still_use_exact_legal_name_in_identity_text(self):
        profile = {
            "organisation_number": "985628572",
            "name": "Netsolution Viken AS",
            "evidence": {"website": {
                "status": "available",
                "source_url": "https://netsolution.no/",
                "value": {
                    "final_url": "https://netsolution.no/",
                    "title": "IT services",
                    "identity_text_excerpt": "Useful services. Netsolution Viken AS, Kobbervikdalen 75 A, 3036 Drammen.",
                    "main_text_excerpt": "Useful services for customers." * 6,
                },
            }},
        }
        self.assertTrue(assess_website_identity(profile)["publishable"])


class SocialOwnershipAuditTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "organisation_number": "923609016",
            "name": "Norsk Fiskeeksport AS",
            "evidence": {"website": {
                "status": "available",
                "source_url": "https://norsk-fiskeeksport.no/",
                "value": {"final_url": "https://www.norsk-fiskeeksport.no/"},
            }},
        }
        self.link = {
            "platform": "linkedin",
            "url": "https://linkedin.com/company/norsk-fiskeeksport",
        }

    def evidence(self, **overrides):
        result = {
            "source_url": "https://www.linkedin.com/company/norsk-fiskeeksport/?trk=official",
            "profile_name": "Norsk Fiskeeksport AS",
            "official_website_url": "https://norsk-fiskeeksport.no/",
            "content_sha256": "a" * 64,
            "retrieved_at": "2026-10-23T00:00:00Z",
            "verification_method": "official_platform_api",
        }
        result.update(overrides)
        return result

    def test_matching_handle_alone_is_not_ownership_proof(self):
        result = assess_social_identity(self.profile, self.link)
        self.assertFalse(result["publishable"])
        self.assertFalse(result["ownership_verified"])
        self.assertTrue(any("no independent profile evidence" in reason for reason in result["reasons"]))

    def test_profile_name_reciprocal_domain_and_source_provenance_allow_publication(self):
        link = {**self.link, "verification_evidence": self.evidence()}
        result = assess_social_identity(self.profile, link)
        self.assertTrue(result["publishable"])
        self.assertTrue(result["ownership_verified"])
        self.assertTrue(all(result["verification_checks"].values()))

    def test_profile_for_another_official_domain_is_quarantined(self):
        link = {**self.link, "verification_evidence": self.evidence(official_website_url="https://parent-group.no/")}
        result = assess_social_identity(self.profile, link)
        self.assertFalse(result["publishable"])
        self.assertFalse(result["verification_checks"]["profile_links_back_to_official_site"])

    def test_missing_hash_or_unapproved_acquisition_method_is_quarantined(self):
        link = {**self.link, "verification_evidence": self.evidence(content_sha256="missing", verification_method="unknown_scraper")}
        result = assess_social_identity(self.profile, link)
        self.assertFalse(result["publishable"])
        self.assertFalse(result["verification_checks"]["profile_evidence_has_sha256"])
        self.assertFalse(result["verification_checks"]["profile_evidence_has_permitted_method"])

    def test_site_footer_social_link_stays_discovered_but_is_not_published(self):
        website = {
            "status": "available",
            "source_url": "https://norsk-fiskeeksport.no/",
            "value": {
                "final_url": "https://norsk-fiskeeksport.no/",
                "title": "Norsk Fiskeeksport AS",
                "main_text_excerpt": "Norsk Fiskeeksport exports seafood to global customers. " * 3,
                "social_links": [self.link],
            },
        }
        result = apply_website_identity_gate(self.profile, website)
        self.assertTrue(result["assessment"]["publishable"])
        self.assertEqual(result["website"]["value"]["social_links"], [])
        self.assertEqual(result["website"]["value"]["discovered_social_links"], [self.link])
        self.assertEqual(result["quarantined_social_links"], 1)

    def test_gate_publishes_only_profile_with_independent_verification(self):
        verified_link = {**self.link, "verification_evidence": self.evidence()}
        website = {
            "status": "available",
            "source_url": "https://norsk-fiskeeksport.no/",
            "value": {
                "final_url": "https://norsk-fiskeeksport.no/",
                "title": "Norsk Fiskeeksport AS",
                "main_text_excerpt": "Norsk Fiskeeksport exports seafood to global customers. " * 3,
                "social_links": [self.link, verified_link],
            },
        }
        result = apply_website_identity_gate(self.profile, website)
        self.assertEqual(result["website"]["value"]["social_links"], [self.link])
        self.assertEqual(result["quarantined_social_links"], 1)


if __name__ == "__main__":
    unittest.main()
