from __future__ import annotations

import hashlib
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.build_evidence_briefs import _record_source, build_brief  # noqa: E402


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def base_profile():
    return {
        "organisation_number": "923609016",
        "name": "Nordic Door AS",
        "legal_form": "AS",
        "municipality": "OSLO",
        "industry_label": "Manufacture of doors",
        "employees": 18,
        "evidence": {
            "registry": {
                "status": "available", "source_url": "https://data.brreg.no/entity/923609016",
                "retrieved_at": "2026-10-31T00:00:00Z", "content_sha256": _sha("registry"),
            },
            "financials": {
                "status": "available", "source_url": "https://data.brreg.no/regnskap/923609016",
                "retrieved_at": "2026-10-31T00:00:00Z", "content_sha256": _sha("financials"),
                "value": {"records": [{
                    "period": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
                    "currency": "NOK", "revenue": 5161209, "operating_result": 95540,
                }]},
            },
            "website": {"status": "not_found", "value": {}},
        },
    }


def nav_job(**changes):
    item = {
        "id": "nav-job-1", "organisation_number": "923609016", "platform": "job_board",
        "signal_type": "job_posting", "source_url": "https://arbeidsplassen.nav.no/stillinger/stilling/1",
        "retrieved_at": "2026-11-01T00:00:00Z", "published_at": "2026-10-20T09:00:00Z",
        "content_sha256": _sha("nav job"), "exact_entity": True,
        "identity_proof": [{
            "type": "nav_employer_organisation_number",
            "expected_parent_organisation_number": "923609016",
            "observed_employer_organisation_number": "923609016",
            "match": "exact",
        }],
        "acquisition_mode": "official_api", "rights_status": "approved",
        "source_class": "official_nav_job_feed", "evidence_span": "Production Engineer — Nordic Door AS",
        "metrics": {"title": "Production Engineer", "status": "ACTIVE", "expires_at": "2026-12-01T00:00:00Z"},
    }
    item.update(changes)
    return item


def test_registry_and_financial_facts_are_context_not_external_relevance():
    result = build_brief(base_profile(), as_of="2026-11-01")
    assert result["why_relevant"]["status"] == "context_only"
    assert result["why_relevant"]["indicators"] == []
    assert len(result["company_context"]["items"]) >= 2


def test_financial_period_and_currency_are_formatted_naturally():
    result = build_brief(base_profile(), as_of="2026-11-01")
    summary = result["company_summary"]
    assert "FY2023 (1 Jan 2023–31 Dec 2023)" in summary
    assert "NOK 5,161,209" in summary
    assert "NOK 95,540" in summary
    assert "{'fraDato'" not in summary


def test_only_active_dated_non_expired_exact_employer_job_is_relevance():
    result = build_brief(base_profile(), [nav_job()], as_of="2026-11-01")
    assert result["why_relevant"]["status"] == "verified_hiring_signals_available"
    assert result["why_relevant"]["indicators"][0]["basis"] == "exact_entity_job_posting"
    assert any(source["module"] == "job_board" for source in result["sources"])


def test_undated_inactive_and_expired_jobs_do_not_count():
    undated = nav_job(published_at=None)
    inactive = nav_job(metrics={"title": "Production Engineer", "status": "INACTIVE", "expires_at": "2026-12-01T00:00:00Z"})
    expired = nav_job(metrics={"title": "Production Engineer", "status": "ACTIVE", "expires_at": "2026-10-31T00:00:00Z"})
    result = build_brief(base_profile(), [undated, inactive, expired], as_of="2026-11-01")
    assert result["why_relevant"]["status"] == "context_only"
    assert result["why_relevant"]["indicators"] == []


def test_wrong_entity_or_unapproved_job_does_not_count():
    wrong = nav_job(exact_entity=False)
    experimental = nav_job(acquisition_mode="rights_review_experiment", rights_status="review_required")
    result = build_brief(base_profile(), [wrong, experimental], as_of="2026-11-01")
    assert result["why_relevant"]["indicators"] == []


def test_company_owned_dated_activity_is_separate_from_independent_news():
    update = {
        "id": "site-update-1", "organisation_number": "923609016", "platform": "company_site",
        "signal_type": "public_post", "source_url": "https://nordicdoor.no/news/new-factory",
        "retrieved_at": "2026-11-01T00:00:00Z", "published_at": "2026-10-15T00:00:00Z",
        "content_sha256": _sha("site news"), "exact_entity": True,
        "identity_proof": [{"type": "website_identity_gate", "status": "exact"}],
        "acquisition_mode": "permitted_public_page", "rights_status": "approved",
        "source_class": "company_site", "evidence_span": "Nordic Door opens a new production line.",
    }
    result = build_brief(base_profile(), [update], as_of="2026-11-01")
    assert result["why_relevant"]["status"] == "company_owned_activity_only"
    assert result["why_relevant"]["indicators"][0]["basis"] == "dated_company_owned_update"
    assert "not independent news or sentiment" in result["why_relevant"]["indicators"][0]["reason"]


def test_company_owned_website_description_is_context_not_independent_validation():
    row = base_profile()
    row["evidence"]["website"] = {
        "status": "available", "source_url": "https://nordicdoor.no/",
        "retrieved_at": "2026-11-01T00:00:00Z", "content_sha256": _sha("site"),
        "value": {"identity_assessment": {"publishable": True, "status": "exact"}, "description": "Manufactures doors."},
    }
    result = build_brief(row, as_of="2026-11-01")
    assert result["why_relevant"]["status"] == "context_only"
    assert all(item["basis"] != "company_reported_claim" for item in result["why_relevant"]["indicators"])
    assert any(item["basis"] == "company_reported_claim" for item in result["company_context"]["items"])


def test_review_website_is_not_a_company_reported_claim_even_if_publishable_flag_is_true():
    row = base_profile()
    row["evidence"]["website"] = {
        "status": "available", "source_url": "https://nordicdoor.no/",
        "retrieved_at": "2026-11-01T00:00:00Z", "content_sha256": _sha("site"),
        "value": {"identity_assessment": {"publishable": True, "status": "review"}, "description": "Quarantined claim."},
    }
    result = build_brief(row, as_of="2026-11-01")
    assert "Quarantined claim" not in result["company_summary"]
    assert not any(item["basis"] == "company_reported_claim" for item in result["company_context"]["items"])


def test_brief_source_url_parser_rejects_malformed_authority_and_nonstandard_ports():
    base = {
        "status": "available", "retrieved_at": "2026-11-01T00:00:00Z", "content_sha256": _sha("url"),
    }
    for url in (
        "https://", "https:///path", "https://user:pass@example.test/", "https://example.test:8443/",
        "http://example.test:8080/", "https://bad host.test/path", "https://example.test:bad/",
        "https://example.test:99999/", "https://exa_mple.test/",
    ):
        assert _record_source({**base, "source_url": url}, "registry", "official_registry_fact") is None, url


def test_brief_source_url_parser_accepts_standard_http_https_and_ipv6_authorities():
    base = {"status": "available", "retrieved_at": "2026-11-01T00:00:00Z", "content_sha256": _sha("url")}
    for url in ("https://data.brreg.no/entity/1", "https://data.brreg.no:443/entity/1", "http://example.test:80/entity/1", "https://[2001:db8::1]/entity/1"):
        assert _record_source({**base, "source_url": url}, "registry", "official_registry_fact") is not None, url


def test_unavailable_or_unhashed_registry_data_is_not_used_as_source():
    row = base_profile()
    row["evidence"]["registry"]["status"] = "source_error"
    result = build_brief(row, as_of="2026-11-01")
    assert not any(source["module"] == "registry" for source in result["sources"])
    assert not any(item["basis"] == "official_registry_fact" for item in result["company_context"]["items"])
