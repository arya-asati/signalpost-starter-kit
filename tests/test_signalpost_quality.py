from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from bs4 import BeautifulSoup  # noqa: E402

from norway_company_agent.website import _priority_links, _publication_metadata  # noqa: E402
from norway_company_agent.external_footprint import publishable_observation  # noqa: E402
from scripts.build_evidence_briefs import build_brief  # noqa: E402
from scripts.extract_company_site_news import observations as extract_site_news  # noqa: E402
from scripts.run_nav_jobs_connector import (  # noqa: E402
    build_job_observation,
    build_website_candidate,
    is_name_candidate,
    normalise_company_name,
)
from scripts.verify_nav_employer_websites import verify_candidates


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_career_and_jobs_pages_are_prioritised_on_the_same_site():
    soup = BeautifulSoup(
        '<a href="/karriere">Karriere</a>'
        '<a href="/ledige-stillinger">Ledige stillinger</a>'
        '<a href="https://other.example/careers">External career</a>'
        '<a href="/produkt">Products</a>',
        "html.parser",
    )
    links = _priority_links("https://example.no/", soup)
    assert links[:2] == ["https://example.no/karriere", "https://example.no/ledige-stillinger"]
    assert all("other.example" not in url for url in links)


def test_publication_metadata_prefers_article_published_date_not_modified_date():
    soup = BeautifulSoup(
        '<html><head>'
        '<meta property="article:published_time" content="2026-08-23T10:30:00Z">'
        '<meta property="article:modified_time" content="2026-08-24T10:30:00Z">'
        '</head><body></body></html>',
        "html.parser",
    )
    result = _publication_metadata(soup)
    assert result["published_at"] == "2026-08-23T10:30:00Z"
    assert "published_time" in result["publication_date_source"]
    assert result["date_modified"] == "2026-08-24T10:30:00Z"


def test_jsonld_news_article_publication_date_is_captured():
    soup = BeautifulSoup(
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"NewsArticle",'
        '"headline":"New contract","datePublished":"2026-07-01T09:00:00Z"}'
        '</script>',
        "html.parser",
    )
    result = _publication_metadata(soup)
    assert result["published_at"] == "2026-07-01T09:00:00Z"
    assert result["publication_date_source"].startswith("jsonld:")


def test_news_extractor_requires_exact_identity_article_date_excerpt_and_hash():
    profile = {
        "organisation_number": "923609016",
        "evidence": {"website": {
            "status": "available",
            "retrieved_at": "2026-10-31T00:00:00Z",
            "value": {
                "identity_assessment": {"publishable": True, "status": "exact", "score": 1.0, "method": "test"},
                "pages": [{
                    "url": "https://example.no/aktuelt/new-contract",
                    "title": "New contract",
                    "main_text_excerpt": "The company announced a new contract.",
                    "published_at": "2026-10-20T09:00:00Z",
                    "publication_date_source": "meta:article:published_time",
                    "content_sha256": "a" * 64,
                }],
            },
        }},
    }
    rows = extract_site_news(profile, now=NOW)
    assert len(rows) == 1
    assert rows[0]["published_at"].startswith("2026-10-20")
    assert publishable_observation(rows[0])

    profile["evidence"]["website"]["value"]["pages"][0].pop("published_at")
    assert extract_site_news(profile, now=NOW) == []


def test_news_extractor_abstains_on_archive_pages_and_wrong_site_identity():
    profile = {
        "organisation_number": "923609016",
        "evidence": {"website": {
            "status": "available",
            "retrieved_at": "2026-10-31T00:00:00Z",
            "value": {
                "identity_assessment": {"publishable": True},
                "pages": [{
                    "url": "https://example.no/aktuelt",
                    "title": "News archive",
                    "main_text_excerpt": "Latest updates.",
                    "published_at": "2026-10-20T09:00:00Z",
                    "content_sha256": "b" * 64,
                }],
            },
        }},
    }
    assert extract_site_news(profile, now=NOW) == []
    profile["evidence"]["website"]["value"]["identity_assessment"]["publishable"] = False
    profile["evidence"]["website"]["value"]["pages"][0]["url"] = "https://example.no/aktuelt/article"
    assert extract_site_news(profile, now=NOW) == []


def nav_profile(org="923609016", name="Nordic Door AS"):
    return {"organisation_number": org, "name": name}


def nav_item(status="ACTIVE", name="Nordic Door AS"):
    return {
        "id": "feed-123",
        "title": "Production Engineer",
        "url": "/api/v1/feedentry/job-uuid-123",
        "_feed_entry": {
            "uuid": "job-uuid-123",
            "status": status,
            "businessName": name,
            "sistEndret": "2026-10-21T10:00:00Z",
        },
    }


def nav_detail(org="923609016", status="ACTIVE", published="2026-10-20T09:00:00Z", expires="2026-12-01T00:00:00Z"):
    return {
        "uuid": "job-uuid-123",
        "status": status,
        "json": {
            "uuid": "job-uuid-123",
            "published": published,
            "expires": expires,
            "title": "Production Engineer",
            "link": "https://arbeidsplassen.nav.no/stillinger/stilling/job-uuid-123",
            "employer": {"name": "Nordic Door AS", "orgnr": org},
            "workLocations": [{"city": "Oslo", "county": "Oslo"}],
            "engagementtype": "Permanent",
        },
    }


def test_nav_job_requires_exact_employer_organisation_number():
    raw = b'{"uuid":"job-uuid-123"}'
    row = build_job_observation(
        nav_profile(), nav_item(), nav_detail(), raw,
        retrieved_at="2026-11-01T00:00:00Z", now=NOW,
    )
    assert row is not None
    assert row["organisation_number"] == "923609016"
    assert row["identity_proof"][0]["match"] == "exact"
    assert row["acquisition_mode"] == "official_api"
    assert row["rights_status"] == "approved"
    assert publishable_observation(row)

    wrong = build_job_observation(
        nav_profile(), nav_item(), nav_detail(org="999999999"), raw,
        retrieved_at="2026-11-01T00:00:00Z", now=NOW,
    )
    assert wrong is None


def test_nav_job_accepts_only_brreg_registered_subunits_of_the_parent_entity():
    parent = nav_profile()
    parent["evidence"] = {
        "locations": {
            "status": "available",
            "source_url": "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=923609016",
            "retrieved_at": "2026-10-31T00:00:00Z",
            "content_sha256": "c" * 64,
            "value": {"locations": [{"organisation_number": "987654321", "name": "Nordic Door AS Oslo"}]},
        }
    }
    raw = b'{"uuid":"job-uuid-123","employer":{"orgnr":"987654321"}}'
    detail = nav_detail(org="987654321")
    # This test also exercises homepage-candidate extraction, so provide the
    # optional NAV homepage field. The production code correctly abstains
    # when NAV does not supply an employer homepage.
    detail["json"]["employer"]["homepage"] = "nordicdoor.no"
    row = build_job_observation(
        parent, nav_item(name="Nordic Door AS Oslo"), detail, raw,
        retrieved_at="2026-11-01T00:00:00Z", now=NOW,
    )
    assert row is not None
    assert row["organisation_number"] == "923609016"
    assert row["metrics"]["employer_organisation_number"] == "987654321"
    assert row["identity_proof"][1]["type"] == "brreg_registered_subunit_relationship"
    assert row["identity_proof"][1]["match"] == "exact_parent_child"
    assert publishable_observation(row)

    candidate = build_website_candidate(
        parent, row, detail, raw, retrieved_at="2026-11-01T00:00:00Z"
    )
    assert candidate is not None
    assert candidate["organisation_number"] == "923609016"
    assert candidate["parent_organisation_number"] == "923609016"
    assert candidate["exact_employer_organisation_number"] == "987654321"

    unrelated = build_job_observation(
        parent, nav_item(name="Nordic Door AS Oslo"), nav_detail(org="999999999"), raw,
        retrieved_at="2026-11-01T00:00:00Z", now=NOW,
    )
    assert unrelated is None


def test_nav_name_prefilter_includes_registered_subunit_names():
    parent = nav_profile()
    parent["evidence"] = {
        "locations": {
            "status": "available",
            "source_url": "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=923609016",
            "retrieved_at": "2026-10-31T00:00:00Z",
            "content_sha256": "d" * 64,
            "value": {"locations": [{"organisation_number": "987654321", "name": "Nordic Door AS Oslo"}]},
        }
    }
    from scripts.run_nav_jobs_connector import profile_matches_feed_names
    assert profile_matches_feed_names(parent, ["Nordic Door AS Oslo"])
    assert not profile_matches_feed_names(parent, ["Northern Window AS"])


def test_nav_job_rejects_inactive_expired_future_and_undated_ads():
    raw = b'{"uuid":"job-uuid-123"}'
    common = {"retrieved_at": "2026-11-01T00:00:00Z", "now": NOW}
    assert build_job_observation(nav_profile(), nav_item("INACTIVE"), nav_detail(), raw, **common) is None
    assert build_job_observation(nav_profile(), nav_item(), nav_detail(expires="2026-10-01T00:00:00Z"), raw, **common) is None
    assert build_job_observation(nav_profile(), nav_item(), nav_detail(published=None), raw, **common) is None
    assert build_job_observation(nav_profile(), nav_item(), nav_detail(published="2026-12-15T00:00:00Z"), raw, **common) is None


def test_rotating_public_token_jobs_remain_quarantined():
    row = build_job_observation(
        nav_profile(), nav_item(), nav_detail(), b'{"uuid":"job-uuid-123"}',
        retrieved_at="2026-11-01T00:00:00Z", experimental_token=True, now=NOW,
    )
    assert row is not None
    assert row["acquisition_mode"] == "rights_review_experiment"
    assert row["rights_status"] == "review_required"
    assert not publishable_observation(row)


def test_name_match_is_only_a_candidate_filter_and_handles_legal_suffixes():
    assert normalise_company_name("NORDIC DØR AS") == "nordic dor"
    assert is_name_candidate("Nordic Door AS", "Nordic Door")
    assert not is_name_candidate("Nordic Door AS", "Northern Window AS")


def test_evidence_brief_has_direct_sources_and_abstains_on_weak_data():
    digest = hashlib.sha256(b"official record").hexdigest()
    profile = {
        "organisation_number": "923609016",
        "name": "Nordic Door AS",
        "legal_form": "AS",
        "municipality": "OSLO",
        "industry_code": "16.230",
        "industry_label": "Manufacture of other builders' carpentry",
        "employees": 18,
        "evidence": {
            "registry": {"status": "available", "source_url": "https://data.brreg.no/entity/923609016", "retrieved_at": "2026-10-31T00:00:00Z", "content_sha256": digest},
            "financials": {"status": "not_found", "value": {"records": []}},
            "website": {"status": "not_found", "value": {}},
        },
    }
    result = build_brief(profile)
    assert "Nordic Door AS" in result["company_summary"]
    assert result["why_relevant"]["status"] == "context_only"
    assert result["why_relevant"]["indicators"] == []
    assert all(item["source_url"].startswith("https://") and len(item["content_sha256"]) == 64 for item in result["sources"])

    profile["evidence"]["registry"].pop("content_sha256")
    weak = build_brief(profile)
    assert weak["why_relevant"]["status"] == "insufficient_evidence"
    assert weak["sources"] == []


def test_nav_homepage_is_only_a_candidate_until_independent_identity_gate():
    profile = nav_profile()
    detail = nav_detail()
    detail["json"]["employer"]["homepage"] = "nordicdoor.no"
    raw = b'{"uuid":"job-uuid-123","employer":{"orgnr":"923609016"}}'
    job = build_job_observation(
        profile, nav_item(), detail, raw,
        retrieved_at="2026-11-01T00:00:00Z", now=NOW,
    )
    candidate = build_website_candidate(
        profile, job, detail, raw,
        retrieved_at="2026-11-01T00:00:00Z",
    )
    assert candidate["candidate_url"] == "https://nordicdoor.no"
    assert candidate["candidate_status"] == "discovered_not_verified"
    assert candidate["exact_employer_organisation_number"] == profile["organisation_number"]

    detail["json"]["employer"]["orgnr"] = "999999999"
    assert build_website_candidate(profile, job, detail, raw, retrieved_at="2026-11-01T00:00:00Z") is None


def test_experimental_nav_homepage_candidates_are_never_fetched_or_promoted():
    profile = {"organisation_number": "923609016", "name": "Nordic Door AS", "evidence": {}}
    candidate = {
        "organisation_number": "923609016",
        "candidate_url": "https://nordicdoor.no",
        "candidate_status": "discovered_not_verified",
        "exact_employer_organisation_number": "923609016",
        "source_url": "https://arbeidsplassen.nav.no/job/1",
        "retrieved_at": "2026-11-01T00:00:00Z",
        "content_sha256": "a" * 64,
        "identity_proof": [{"type": "nav_employer_organisation_number", "match": "exact"}],
        "acquisition_mode": "rights_review_experiment",
        "rights_status": "review_required",
    }
    def must_not_fetch(*args, **kwargs):
        raise AssertionError("Experimental candidate must not be fetched")
    rows, report = verify_candidates([profile], [candidate], fetch_fn=must_not_fetch, min_interval=0)
    assert report["attempts"] == 0
    assert report["skipped_experimental_or_unapproved"] == 1
    assert rows[0].get("website") is None


def test_nav_homepage_is_promoted_only_after_exact_site_identity_gate_passes():
    profile = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "website": None, "evidence": {}}
    candidate = {
        "organisation_number": "923609016",
        "candidate_url": "https://norskfiske.no/",
        "candidate_status": "discovered_not_verified",
        "exact_employer_organisation_number": "923609016",
        "source_url": "https://arbeidsplassen.nav.no/job/1",
        "retrieved_at": "2026-11-01T00:00:00Z",
        "content_sha256": "a" * 64,
        "identity_proof": [{"type": "nav_employer_organisation_number", "match": "exact"}],
        "acquisition_mode": "official_api",
        "rights_status": "approved",
    }
    def fetch_exact(url, *, timeout):
        return ({
            "field": "website", "status": "available", "source_type": "registry_linked_company_website",
            "source_class": "registry_linked_company_website", "source_url": url,
            "retrieved_at": "2026-11-01T00:00:00Z", "content_sha256": "b" * 64,
            "value": {"final_url": url, "title": "Norsk Fiskeeksport AS", "main_text_excerpt": "Norsk Fiskeeksport AS company information.", "pages": []},
        }, {"requests": 2, "bytes": 500, "latencies_ms": [10]})
    rows, report = verify_candidates([profile], [candidate], fetch_fn=fetch_exact, min_interval=0)
    assert report["verified_and_promoted"] == 1
    assert rows[0]["website"] == "https://norskfiske.no/"
    assert rows[0]["evidence"]["website"]["source_class"] == "independently_verified_company_website"


def test_nav_homepage_parent_group_site_is_quarantined_not_promoted():
    profile = {"organisation_number": "923609016", "name": "Exact Subsidiary AS", "website": None, "evidence": {}}
    candidate = {
        "organisation_number": "923609016", "candidate_url": "https://parent.example/",
        "candidate_status": "discovered_not_verified", "exact_employer_organisation_number": "923609016",
        "source_url": "https://arbeidsplassen.nav.no/job/1", "retrieved_at": "2026-11-01T00:00:00Z",
        "content_sha256": "a" * 64, "identity_proof": [{"type": "nav_employer_organisation_number", "match": "exact"}],
        "acquisition_mode": "official_api", "rights_status": "approved",
    }
    def fetch_parent(url, *, timeout):
        return ({
            "field": "website", "status": "available", "source_type": "registry_linked_company_website",
            "source_class": "registry_linked_company_website", "source_url": url,
            "retrieved_at": "2026-11-01T00:00:00Z", "content_sha256": "c" * 64,
            "value": {"final_url": url, "title": "Parent Group", "main_text_excerpt": "Parent Group portfolio information.", "pages": []},
        }, {"requests": 2, "bytes": 500, "latencies_ms": [10]})
    rows, report = verify_candidates([profile], [candidate], fetch_fn=fetch_parent, min_interval=0)
    assert report["quarantined_identity_not_proven"] == 1
    assert rows[0].get("website") is None
    assert rows[0]["website_discovery_attempts"][-1]["status"] == "quarantined_identity_not_proven"
