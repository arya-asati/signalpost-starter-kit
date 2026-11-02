from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.external_footprint import publishable_observation, validate_observation  # noqa: E402
from norway_company_agent.output_contract import contract_envelope, validate_contract_envelopes  # noqa: E402
from scripts.build_evidence_briefs import build_brief  # noqa: E402
from scripts.build_prototype import build, compact  # noqa: E402


ORG = "923609016"
NAME = "Nordic Door AS"
HASH = "a" * 64
NOW = "2026-11-01T12:00:00Z"


def profile(org: str = ORG, name: str = NAME) -> dict:
    return {
        "organisation_number": org,
        "name": name,
        "legal_form": "AS",
        "employees": None,
        "municipality": "Oslo",
        "municipality_number": "0301",
        "industry_code": "16.230",
        "industry_label": "Manufacture of other builders' carpentry and joinery",
        "website": None,
        "bankrupt": False,
        "liquidating": False,
        "latest_submitted_accounts": "2025",
        "evidence": {
            "registry": {
                "field": "registry",
                "status": "available",
                "source_type": "official_registry_bulk",
                "source_class": "official_registry_bulk",
                "source_url": f"https://registry.example/{org}",
                "retrieved_at": "2026-10-31T00:00:00Z",
                "content_sha256": HASH,
                "value": {"organisation_number": org, "name": name},
            },
            "financials": {
                "field": "financials",
                "status": "available",
                "source_type": "official_annual_accounts",
                "source_class": "official_annual_accounts",
                "source_url": f"https://accounts.example/{org}",
                "retrieved_at": "2026-10-31T00:00:00Z",
                "content_sha256": "b" * 64,
                "value": {"records": [{
                    "currency": "NOK",
                    "revenue": 1000,
                    "operating_result": 100,
                    "annual_result": 80,
                    "period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
                }]},
            },
            "registry_live": {"status": "available", "source_url": f"https://registry.example/{org}"},
            "accounting_obligation": {"status": "available", "source_url": f"https://registry.example/{org}"},
            "roles": {"status": "available", "source_url": f"https://registry.example/{org}", "value": {"roles": []}},
            "locations": {"status": "available", "source_url": f"https://registry.example/{org}", "value": {"locations": []}},
            "group": {"status": "not_found", "value": None},
            "website": {"status": "not_found", "value": None},
        },
    }


def independent_mention(**changes) -> dict:
    item = {
        "id": "news-1",
        "organisation_number": ORG,
        "platform": "news",
        "signal_type": "public_mention",
        "source_url": "https://news.example/nordic-door-contract",
        "retrieved_at": "2026-10-31T12:00:00Z",
        "published_at": "2026-10-20T09:00:00Z",
        "content_sha256": HASH,
        "exact_entity": True,
        "identity_proof": [{"type": "manual_exact_entity_review", "legal_name": NAME, "basis": "Exact legal entity confirmed in article"}],
        "acquisition_mode": "permitted_public_page",
        "rights_status": "approved",
        "source_class": "public_news",
        "evidence_span": "Nordic Door AS announced a new factory contract.",
    }
    item.update(changes)
    return item


def linkedin_profile(**changes) -> dict:
    item = {
        "id": "linkedin-profile-1",
        "organisation_number": ORG,
        "platform": "linkedin",
        "signal_type": "profile_metrics",
        "source_url": "https://www.linkedin.com/company/nordic-door",
        "retrieved_at": "2026-10-31T12:00:00Z",
        "content_sha256": HASH,
        "exact_entity": True,
        "identity_proof": [{"type": "linkedin_structured_company_identity", "value": {
            "publishable_candidate": True,
            "resolved_url": "https://www.linkedin.com/company/nordic-door",
            "exact_legal_name_core": True,
            "official_domain": "nordicdoor.example",
            "linkedin_website_domain": "another.example",
            "reverse_domain_match": False,
        }}],
        "acquisition_mode": "permitted_public_page",
        "rights_status": "approved",
        "source_class": "professional_network",
        "evidence_span": "Nordic Door AS company profile metrics snapshot.",
        "metrics": {"followers": 12, "visible_employees": 5},
    }
    item.update(changes)
    return item


def nav_job(**changes) -> dict:
    item = {
        "id": "nav-job-1",
        "organisation_number": ORG,
        "platform": "job_board",
        "signal_type": "job_posting",
        "source_url": "https://arbeidsplassen.nav.no/stillinger/stilling/123",
        "retrieved_at": "2026-10-31T12:00:00Z",
        "published_at": "2026-10-20T09:00:00Z",
        "content_sha256": HASH,
        "exact_entity": True,
        "identity_proof": [{
            "type": "nav_employer_organisation_number",
            "expected_parent_organisation_number": ORG,
            "observed_employer_organisation_number": ORG,
            "match": "exact",
        }],
        "acquisition_mode": "official_api",
        "rights_status": "approved",
        "source_class": "official_nav_job_feed",
        "evidence_span": "Production Engineer — Nordic Door AS",
        "metrics": {"title": "Production Engineer", "status": "ACTIVE", "expires_at": "2026-12-01T00:00:00Z"},
    }
    item.update(changes)
    return item


def company_site_update(**changes) -> dict:
    item = {
        "id": "site-update-1",
        "organisation_number": ORG,
        "platform": "company_site",
        "signal_type": "public_post",
        "source_url": "https://nordicdoor.example/news/factory",
        "retrieved_at": "2026-10-31T12:00:00Z",
        "published_at": "2026-10-20T09:00:00Z",
        "content_sha256": HASH,
        "exact_entity": True,
        "identity_proof": [{"type": "website_identity_gate", "status": "exact", "score": 0.95}],
        "acquisition_mode": "permitted_public_page",
        "rights_status": "approved",
        "source_class": "company_site",
        "evidence_span": "Nordic Door opened a new factory line.",
    }
    item.update(changes)
    return item


def test_no_linkedin_profile_means_zero_qualifying_profiles():
    result = compact(profile())
    assert result["external"]["linkedin"]["available"] is False
    html = build([profile()], None)
    assert "1-company showcase · 0 qualifying LinkedIn profiles" in html
    assert '"qualifying_linkedin_profiles": 0' in html


def test_unverified_website_content_is_quarantined_even_without_an_assessment():
    row = profile()
    row["website"] = "https://unverified.example"
    row["evidence"]["website"] = {
        "field": "website",
        "status": "available",
        "source_class": "registry_linked_company_website",
        "source_url": "https://unverified.example",
        "retrieved_at": "2026-10-31T00:00:00Z",
        "content_sha256": HASH,
        "value": {
            "final_url": "https://unverified.example",
            "title": "Unverified legal claim",
            "description": "Raw description with contact person@example.test",
            "main_text_excerpt": "Raw page text with contact person@example.test",
            "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/example"}],
            "discovered_social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/example"}],
            "pages": [{"url": "https://unverified.example/contact", "main_text_excerpt": "person@example.test"}],
            "structured_organisations": [{"name": "Unverified Parent"}],
            "crawl_errors": [{"error": "raw transport detail"}],
        },
    }

    result = compact(row)
    website = result["web"]["value"]
    serialized = json.dumps(result, ensure_ascii=False)

    assert website["identity_quarantined"] is True
    assert website["quarantined_page_count"] == 1
    assert website["quarantined_social_count"] == 1
    assert website["pages"] == []
    assert website["social_links"] == []
    assert website["title"] == "Registry-linked site — identity not verified"
    assert "person@example.test" not in serialized
    assert "Unverified legal claim" not in serialized
    assert "raw transport detail" not in serialized


def test_no_external_evidence_keeps_brief_context_only_without_indicators():
    result = build_brief(profile(), as_of="2026-11-01")
    assert result["why_relevant"]["status"] == "context_only"
    assert result["why_relevant"]["indicators"] == []


def test_website_public_news_label_cannot_make_company_owned_content_independent():
    item = company_site_update(source_class="public_news", signal_type="public_mention")
    result = compact(profile(), [item], as_of=NOW)
    assert result["external"]["independent_mentions"] == []
    assert result["external"]["accepted_observation_count"] == 0
    assert "unsupported platform/source-class/observation-type combination" in result["external"]["quarantined_observations"][0]["reasons"]


def test_indeed_jobs_are_hiring_evidence_and_cannot_be_independent_mentions():
    from norway_company_agent.external_footprint import observation_ownership

    job = {
        **independent_mention(
            platform="indeed", signal_type="job_posting", source_class="job_board",
            source_url="https://www.indeed.com/viewjob?jk=123", published_at="2026-10-20T09:00:00Z",
            identity_proof=[{"type": "indeed_employer_organisation_number", "observed_employer_organisation_number": ORG}],
        ),
        "metrics": {"status": "ACTIVE", "title": "Engineer"},
    }
    assert publishable_observation(job, expected_organisation_number=ORG, as_of=NOW)
    assert observation_ownership(job) == "hiring"
    mislabeled = {**job, "signal_type": "public_mention", "source_class": "public_news"}
    assert not publishable_observation(mislabeled, expected_organisation_number=ORG, as_of=NOW)


def test_linkedin_company_posts_remain_company_owned_and_class_mismatch_is_rejected():
    from norway_company_agent.external_footprint import observation_ownership

    post = linkedin_profile(
        signal_type="public_post", published_at="2026-10-20T09:00:00Z",
        evidence_span="Nordic Door announced a new production line.",
    )
    assert publishable_observation(post, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    assert observation_ownership(post) == "company_owned"
    mislabeled = {**post, "source_class": "public_news", "signal_type": "public_mention"}
    assert not publishable_observation(mislabeled, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_facebook_requires_platform_specific_exact_identity_proof():
    item = {
        **linkedin_profile(platform="facebook", source_url="https://facebook.com/nordicdoor", signal_type="profile_handle", source_class="company_social"),
        "identity_proof": [{"type": "generic_name_match", "value": True}],
    }
    reasons = validate_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    assert any("platform-specific exact-company identity proof" in reason for reason in reasons)


def test_platform_source_class_mismatch_is_not_publishable():
    item = independent_mention(platform="linkedin", source_class="public_news")
    reasons = validate_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    assert "unsupported platform/source-class/observation-type combination" in reasons


def test_nonavailable_website_content_and_legacy_history_are_quarantined():
    row = profile()
    raw = "Quarantined page text contact person@example.test"
    row["evidence"]["website"] = {
        "status": "review",
        "value": {
            "identity_assessment": {"status": "review", "publishable": True, "score": 0.82},
            "title": raw,
            "description": raw,
            "main_text_excerpt": raw,
            "social_links": [{"platform": "facebook", "url": "https://facebook.com/private"}],
            "pages": [{"main_text_excerpt": raw}],
        },
    }
    row["change_history"] = [{"field": "website.description", "old_value": raw, "new_value": raw}]
    result = compact(row)
    serialized = json.dumps(result, ensure_ascii=False)
    assert raw not in serialized
    assert "person@example.test" not in serialized
    assert result["web"]["value"]["identity_quarantined"] is True
    assert result["web"]["value"]["social_links"] == []
    assert result["changes"][0]["old_value"] == {"redacted": True, "present": True}


def test_missing_website_status_with_populated_content_is_quarantined():
    row = profile()
    row["evidence"]["website"] = {"value": {"description": "Never publish this unverified text", "pages": [{"main_text_excerpt": "secret"}]}}
    result = compact(row)
    assert result["web"]["value"]["identity_quarantined"] is True
    assert "Never publish this unverified text" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_stale_and_future_public_activity_are_rejected_with_fixed_as_of():
    stale = independent_mention(published_at="2025-10-31T00:00:00Z")
    future = independent_mention(published_at="2026-11-02T00:00:00Z")
    assert "public activity is stale" in validate_observation(stale, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    assert "publication date is in the future" in validate_observation(future, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    assert publishable_observation(independent_mention(published_at="2026-10-20T00:00:00Z"), expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_prototype_hides_financials_unless_module_is_available_and_preserves_zero_vs_null():
    row = profile()
    row["evidence"]["financials"]["status"] = "source_error"
    row["evidence"]["financial_history"] = {"status": "blocked", "value": {"pdfs": [{"year": 2025, "url": "https://accounts.example/stale.pdf"}]}}
    hidden = compact(row)
    assert hidden["financial"]["records"] == []
    assert hidden["financialHistory"]["pdfs"] == []
    row["evidence"]["financials"]["status"] = "available"
    row["evidence"]["financials"]["value"]["records"] = [{"revenue": 0, "operating_result": None, "annual_result": 0}]
    visible = compact(row)
    assert visible["financial"]["records"][0]["revenue"] == 0
    assert visible["financial"]["records"][0]["operating_result"] is None
    assert visible["financial"]["records"][0]["annual_result"] == 0


def test_exact_entity_flag_alone_cannot_authorize_an_observation():
    item = independent_mention(identity_proof=[])
    reasons = validate_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    assert "missing exact-entity proof" in reasons
    assert not publishable_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_invalid_rights_status_is_rejected():
    item = independent_mention(rights_status="review_required")
    assert "source rights are not approved" in validate_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_missing_content_hash_is_rejected():
    item = independent_mention(content_sha256=None)
    assert "missing content hash" in validate_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_missing_source_url_is_rejected():
    item = independent_mention(source_url="")
    assert "missing or invalid source URL" in validate_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_observation_for_another_company_is_not_selected():
    item = independent_mention(organisation_number="999999999")
    result = compact(profile(), [item])
    assert result["external"]["independent_mentions"] == []
    assert any("organisation number does not match the profile" in reason for row in result["external"]["quarantined_observations"] for reason in row["reasons"])


def test_unverified_nav_observation_remains_quarantined():
    item = nav_job(acquisition_mode="rights_review_experiment", rights_status="review_required")
    result = compact(profile(), [item])
    assert result["external"]["nav_jobs"] == []
    reasons = result["external"]["quarantined_observations"][0]["reasons"]
    assert "acquisition mode is not approved for publication" in reasons
    assert "source rights are not approved" in reasons


def test_company_owned_update_is_kept_separate_from_independent_evidence():
    item = company_site_update()
    result = compact(profile(), [item])
    assert len(result["external"]["company_site_news"]) == 1
    assert result["external"]["independent_mentions"] == []
    assert result["external"]["company_site_news"][0]["evidenceKind"] == "first_party_company_owned"
    brief = build_brief(profile(), [item], as_of="2026-11-01")
    assert brief["why_relevant"]["status"] == "company_owned_activity_only"


def test_valid_independent_observation_is_selected_and_cited():
    item = independent_mention()
    result = compact(profile(), [item])
    assert len(result["external"]["independent_mentions"]) == 1
    assert result["external"]["independent_mentions"][0]["source"] == item["source_url"]
    brief = build_brief(profile(), [item], as_of="2026-11-01")
    assert brief["why_relevant"]["status"] == "independent_external_signals_available"


def test_prototype_counts_follow_only_validated_observations():
    other = profile("123456789", "Other Entity AS")
    item = linkedin_profile()
    html = build(
        [profile(), other],
        None,
        external_by_org={ORG: [item]},
    )
    assert "2-company showcase · 1 qualifying LinkedIn profile" in html
    assert '"qualifying_linkedin_profiles": 1' in html
    assert '"qualifying_external_observations": 1' in html
    assert "22 exact LinkedIn profiles" not in html
    assert "32 exact LinkedIn profiles captured" not in html
    assert "9 sites and 17 handles discovered downstream" not in html


def test_output_contract_envelope_validation_remains_compatible():
    row = profile()

    # Keep this compatibility test focused on one fully attributable
    # official registry evidence record.
    row["evidence"] = {
        "registry": {
            "field": "registry",
            "status": "available",
            "source_type": "official_registry_bulk",
            "source_class": "official_registry_bulk",
            "source_url": (
                "https://data.brreg.no/"
                "enhetsregisteret/api/enheter/lastned/csv"
            ),
            "retrieved_at": "2026-10-31T00:00:00Z",
            "content_sha256": HASH,
            "value": {
                "organisation_number": ORG,
                "name": NAME,
            },
        }
    }

    envelope = contract_envelope(
        row,
        run_id="evidence-integrity-test",
        modules=["registry"],
        started_at="2026-10-31T00:00:00Z",
        completed_at="2026-10-31T00:01:00Z",
    )

    result = validate_contract_envelopes(
        [envelope],
        expected_count=1,
    )

    assert result["passed"], result

def test_valid_linkedin_profile_is_accepted_only_with_structured_identity_proof():
    item = linkedin_profile()
    assert publishable_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    rejected = linkedin_profile(identity_proof=[{"type": "linkedin_structured_company_identity", "value": {
        "publishable_candidate": True,
        "resolved_url": "https://www.linkedin.com/company/other-company",
        "exact_legal_name_core": False,
    }}])
    assert not publishable_observation(rejected, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_valid_linkedin_workforce_post_and_current_job_require_their_own_evidence():
    workforce = linkedin_profile(
        id="linkedin-workforce-1",
        signal_type="workforce_snapshot",
        metrics={"employees": 5, "scope": "profiles-associated-with-company-page"},
    )
    post = linkedin_profile(
        id="linkedin-post-1",
        signal_type="public_post",
        published_at="2026-10-20T09:00:00Z",
        evidence_span="Nordic Door AS announced a new production line.",
        metrics={"date_published": "2026-10-20T09:00:00Z", "likes": 4},
    )
    job = linkedin_profile(
        id="linkedin-job-1",
        signal_type="job_posting",
        source_class="job_board",
        source_url="https://www.linkedin.com/jobs/view/123",
        published_at="2026-10-20T09:00:00Z",
        evidence_span="Production Engineer — Nordic Door AS",
        identity_proof=[
            {"type": "exact_linkedin_company_url_match", "value": "https://www.linkedin.com/company/nordic-door"},
            {"type": "linkedin_company_id_confirmed_by_exact_job_company_url", "value": "123"},
        ],
        metrics={"title": "Production Engineer", "status": "ACTIVE", "expires_at": "2026-12-01T00:00:00Z"},
    )
    assert publishable_observation(workforce, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    assert publishable_observation(post, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    assert publishable_observation(job, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_current_nav_job_requires_matching_parent_number_and_active_details():
    item = nav_job()
    assert publishable_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    wrong_parent = nav_job(identity_proof=[{
        "type": "nav_employer_organisation_number",
        "expected_parent_organisation_number": "999999999",
        "observed_employer_organisation_number": ORG,
        "match": "exact",
    }])
    assert not publishable_observation(wrong_parent, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    inactive = nav_job(metrics={"title": "Production Engineer", "status": "INACTIVE", "expires_at": "2026-12-01T00:00:00Z"})
    assert not publishable_observation(inactive, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_registered_subunit_nav_job_requires_exact_brreg_endpoint_and_parent_query():
    subunit_org = "923609099"
    item = nav_job(
        identity_proof=[
            {
                "type": "nav_employer_organisation_number",
                "expected_parent_organisation_number": ORG,
                "observed_employer_organisation_number": subunit_org,
                "match": "exact_registered_subunit",
            },
            {
                "type": "brreg_registered_subunit_relationship",
                "parent_organisation_number": ORG,
                "subunit_organisation_number": subunit_org,
                "source_url": f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={ORG}",
                "retrieved_at": "2026-10-31T12:00:00Z",
                "content_sha256": "c" * 64,
                "match": "exact_parent_child",
            },
        ]
    )
    assert publishable_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)
    bad_proof = [dict(proof) for proof in item["identity_proof"]]
    bad_proof[1]["source_url"] = f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=999999999"
    rejected = nav_job(identity_proof=bad_proof)
    assert not publishable_observation(rejected, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_unapproved_nonstandard_source_url_is_rejected():
    item = independent_mention(source_url="https://news.example:8443/article")
    assert "missing or invalid source URL" in validate_observation(item, expected_organisation_number=ORG, expected_company_name=NAME, as_of=NOW)


def test_company_site_source_must_match_registered_website_host_when_known():
    item = company_site_update()
    reasons = validate_observation(
        item,
        expected_organisation_number=ORG,
        expected_company_name=NAME,
        expected_website_url="https://other.example/",
        as_of=NOW,
    )
    assert "company-site observation host does not match the registered website" in reasons
