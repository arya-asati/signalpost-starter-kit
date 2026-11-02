from __future__ import annotations

import json
import http.client
import os
import socket
import sys
import urllib.error
from datetime import datetime, timezone

import pytest

from scripts import run_nav_jobs_connector as nav


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
PARENT_ORG = "923609016"
SUBUNIT_ORG = "987654321"
BRREG_LOCATIONS_URL = (
    "https://data.brreg.no/enhetsregisteret/api/underenheter"
    f"?overordnetEnhet={PARENT_ORG}&size=1000"
)


def _profile(*, with_subunit: bool = False, source_url: str = BRREG_LOCATIONS_URL) -> dict:
    profile = {"organisation_number": PARENT_ORG, "name": "Nordlys Elektro AS", "evidence": {}}
    if with_subunit:
        profile["evidence"]["locations"] = {
            "status": "available",
            "source_url": source_url,
            "retrieved_at": "2026-10-31T00:00:00Z",
            "content_sha256": "a" * 64,
            "value": {"locations": [{"organisation_number": SUBUNIT_ORG, "name": "Nordlys Oslo Unit"}]},
        }
    return profile


def _feed_item(
    ad_id: str = "ad-1",
    *,
    status: str = "ACTIVE",
    business_name: str | None = "Nordlys Elara AS",
    title: str = "Senior Software Engineer",
) -> dict:
    entry = {"uuid": ad_id, "status": status, "sistEndret": "2026-10-20T10:00:00Z", "title": title}
    if business_name is not None:
        entry["businessName"] = business_name
    return {
        "id": ad_id,
        "url": f"/api/v1/feedentry/{ad_id}",
        "title": title,
        "_feed_entry": entry,
    }


def _detail(org: str, *, status: str = "ACTIVE") -> dict:
    return {
        "status": status,
        "json": {
            "status": status,
            "employer": {"orgnr": org},
        },
    }


def _install_fake_transport(monkeypatch, items: list[dict], details: dict[str, dict]):
    calls: list[str] = []
    monkeypatch.setenv("NAV_JOB_FEED_TOKEN", "offline-private-test-token")
    monkeypatch.setattr(nav, "_load_token", lambda **kwargs: ("offline-private-test-token", False))

    def fake_http_get(url, headers, timeout, counter):
        calls.append(url)
        counter[0] += 1
        if url == nav.FEED_URL:
            return json.dumps({"items": items}).encode()
        ad_id = url.rsplit("/", 1)[-1]
        return json.dumps(details[ad_id]).encode()

    monkeypatch.setattr(nav, "_http_get", fake_http_get)
    return calls


def _run(monkeypatch, items: list[dict], details: dict[str, dict], **kwargs):
    calls = _install_fake_transport(monkeypatch, items, details)
    options = {"max_pages": 1, "max_details": 20, "min_interval": 0, "now": NOW}
    options.update(kwargs)
    probe_report: dict = {}
    result = nav.run_connector(
        [_profile()],
        identity_probe_report_out=probe_report,
        **options,
    )
    return (*result, probe_report), calls


def _install_custom_probe_transport(monkeypatch, items: list[dict], responses: list[object]):
    calls: list[str] = []
    monkeypatch.setenv("NAV_JOB_FEED_TOKEN", "offline-private-test-token")
    monkeypatch.setattr(nav, "_load_token", lambda **kwargs: ("offline-private-test-token", False))

    def fake_http_get(url, headers, timeout, counter):
        calls.append(url)
        counter[0] += 1
        if url == nav.FEED_URL:
            return json.dumps({"items": items}).encode()
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, bytes) else json.dumps(response).encode()

    monkeypatch.setattr(nav, "_http_get", fake_http_get)
    return calls


def test_diagnostic_identity_probes_are_disabled_by_default(monkeypatch):
    (observations, websites, report, probe_report), calls = _run(
        monkeypatch,
        [_feed_item()],
        {"ad-1": _detail(PARENT_ORG)},
    )

    assert probe_report["enabled"] is False
    assert probe_report["requested"] == 0
    assert probe_report["results"] == []
    assert report["detail_requests"] == 0
    assert observations == []
    assert websites == []
    assert len(calls) == 1  # feed only


def test_diagnostic_identity_probes_have_a_hard_five_ad_cap(monkeypatch):
    items = [_feed_item(f"ad-{index}") for index in range(6)]
    details = {f"ad-{index}": _detail(PARENT_ORG) for index in range(6)}
    (observations, websites, report, probe_report), calls = _run(
        monkeypatch,
        items,
        details,
        diagnostic_identity_probes=5,
    )

    assert probe_report["probe_requests"] == 5
    assert len({item["nav_ad_id"] for item in probe_report["results"]}) == 5
    assert report["detail_requests"] == 5
    assert report["requests_total_including_token"] == 6
    assert len(calls) == 6  # one feed page and five unique details
    assert observations == []
    assert websites == []


def test_more_than_five_probes_are_rejected_before_transport(monkeypatch):
    calls = _install_fake_transport(monkeypatch, [], {})

    with pytest.raises(ValueError, match="between 0 and 5"):
        nav.run_connector([_profile()], diagnostic_identity_probes=6)

    assert calls == []


def test_run_connector_retains_its_three_value_return_contract(monkeypatch):
    _install_fake_transport(monkeypatch, [], {})
    assert len(nav.run_connector([_profile()], max_pages=1, min_interval=0)) == 3


def test_probe_mode_requires_a_separate_diagnostic_report_sink(monkeypatch):
    calls = _install_fake_transport(monkeypatch, [], {})

    with pytest.raises(ValueError, match="identity_probe_report_out is required"):
        nav.run_connector([_profile()], diagnostic_identity_probes=1)

    assert calls == []


def test_inactive_advertisements_are_not_selected_for_probes(monkeypatch):
    (observations, websites, report, probe_report), calls = _run(
        monkeypatch,
        [_feed_item(status="INACTIVE")],
        {"ad-1": _detail(PARENT_ORG)},
        diagnostic_identity_probes=5,
    )

    assert probe_report["selected_unique_ads"] == 0
    assert probe_report["results"] == []
    assert report["detail_requests"] == 0
    assert len(calls) == 1
    assert observations == []
    assert websites == []


def test_inactive_detail_status_is_rejected_for_an_active_feed_entry(monkeypatch):
    (_, _, _, probe_report), _ = _run(
        monkeypatch,
        [_feed_item(status="ACTIVE")],
        {"ad-1": _detail(PARENT_ORG, status="INACTIVE")},
        diagnostic_identity_probes=1,
    )

    result = probe_report["results"][0]
    assert result["result"] == "detail_status_rejected"
    assert result["exact_identity_match"] is False
    assert "observed_employer_organisation_number" not in result


def test_strict_name_matches_are_excluded_from_probe_selection(monkeypatch):
    (observations, websites, report, probe_report), calls = _run(
        monkeypatch,
        [_feed_item(business_name="Nordlys Elektro AS")],
        {"ad-1": _detail(PARENT_ORG)},
        diagnostic_identity_probes=5,
    )

    assert probe_report["selected_unique_ads"] == 0
    assert probe_report["results"] == []
    assert report["detail_requests"] == 1  # the existing strict path only
    assert len(calls) == 2
    assert websites == []
    assert observations == []  # detail lacks publication fields


def test_vacancy_titles_are_never_used_to_select_probes(monkeypatch):
    item = _feed_item(business_name=None, title="Nordlys Elektro AS")
    (observations, websites, report, probe_report), calls = _run(
        monkeypatch,
        [item],
        {"ad-1": _detail(PARENT_ORG)},
        diagnostic_identity_probes=5,
    )

    assert report["name_filter_diagnostics"]["unique_ads_with_title_only"] == 1
    assert probe_report["selected_unique_ads"] == 0
    assert report["detail_requests"] == 0
    assert len(calls) == 1
    assert observations == []
    assert websites == []


def test_probe_reports_exact_parent_number_without_publishing(monkeypatch):
    (observations, websites, report, probe_report), _ = _run(
        monkeypatch,
        [_feed_item()],
        {"ad-1": _detail(PARENT_ORG)},
        diagnostic_identity_probes=1,
    )

    result = probe_report["results"][0]
    assert result["result"] == "exact_parent_organisation_number"
    assert result["observed_employer_organisation_number"] == PARENT_ORG
    assert result["exact_identity_match"] is True
    assert observations == []
    assert websites == []
    assert report["observations"] == 0
    assert "results" not in report
    assert not {"raw_response", "title", "applicant", "contact", "employer_name"} & set(result)


def test_probe_reports_a_brreg_registered_subunit_match(monkeypatch):
    calls = _install_fake_transport(
        monkeypatch,
        [_feed_item()],
        {"ad-1": _detail(SUBUNIT_ORG)},
    )
    probe_report: dict = {}
    observations, websites, report = nav.run_connector(
        [_profile(with_subunit=True)],
        diagnostic_identity_probes=1,
        max_pages=1,
        max_details=20,
        min_interval=0,
        now=NOW,
        identity_probe_report_out=probe_report,
    )

    result = probe_report["results"][0]
    assert result["result"] == "brreg_registered_subunit_match"
    assert result["employer_relationship"] == "registered_subunit"
    assert result["exact_identity_match"] is True
    assert observations == []
    assert websites == []
    assert report["observations"] == 0
    assert len(calls) == 2


def test_unrelated_employer_number_is_reported_as_a_mismatch(monkeypatch):
    (_, _, _, probe_report), _ = _run(
        monkeypatch,
        [_feed_item()],
        {"ad-1": _detail("111222333")},
        diagnostic_identity_probes=1,
    )

    result = probe_report["results"][0]
    assert result["result"] == "unrelated_organisation_number"
    assert result["exact_identity_match"] is False


@pytest.mark.parametrize(
    "source_url",
    [
        "https://brreg.example/enhetsregisteret/api/underenheter?overordnetEnhet=" + PARENT_ORG,
        "https://data.brreg.no/enhetsregisteret/api/enheter?overordnetEnhet=" + PARENT_ORG,
        "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=111222333",
    ],
    ids=["wrong-host", "wrong-endpoint", "wrong-parent-query"],
)
def test_incorrect_brreg_provenance_does_not_register_a_subunit(source_url):
    entities = nav.registered_employer_entities(_profile(with_subunit=True, source_url=source_url))

    assert PARENT_ORG in entities
    assert SUBUNIT_ORG not in entities


def test_locations_timestamp_hash_and_available_status_are_still_required():
    for field, value in (
        ("status", "source_error"),
        ("retrieved_at", ""),
        ("content_sha256", "invalid"),
    ):
        profile = _profile(with_subunit=True)
        profile["evidence"]["locations"][field] = value
        assert SUBUNIT_ORG not in nav.registered_employer_entities(profile)


def test_probe_requests_share_the_detail_budget_and_are_counted(monkeypatch):
    items = [_feed_item(f"ad-{index}") for index in range(3)]
    details = {f"ad-{index}": _detail(PARENT_ORG) for index in range(3)}
    (_, _, report, probe_report), calls = _run(
        monkeypatch,
        items,
        details,
        diagnostic_identity_probes=5,
        max_details=2,
    )

    assert probe_report["probe_requests"] == 2
    assert probe_report["skipped_due_detail_budget"] == 1
    assert report["detail_requests"] == 2
    assert report["requests_total_including_token"] == 3
    assert len(calls) == 3  # feed plus the two budgeted detail requests


def test_probe_mode_fails_closed_without_a_private_token(monkeypatch):
    monkeypatch.delenv("NAV_JOB_FEED_TOKEN", raising=False)
    calls = _install_fake_transport(monkeypatch, [_feed_item()], {"ad-1": _detail(PARENT_ORG)})
    monkeypatch.delenv("NAV_JOB_FEED_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="private NAV consumer token"):
        nav.run_connector(
            [_profile()],
            diagnostic_identity_probes=1,
            experimental_public_token=True,
            identity_probe_report_out={},
        )

    assert calls == []


def test_cli_writes_probe_outcomes_to_a_separate_report(monkeypatch, tmp_path):
    calls = _install_fake_transport(monkeypatch, [_feed_item()], {"ad-1": _detail(PARENT_ORG)})
    profiles_path = tmp_path / "profiles.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    report_path = tmp_path / "connector-report.json"
    probes_path = tmp_path / "identity-probes.json"
    profiles_path.write_text(json.dumps(_profile()) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_nav_jobs_connector.py",
            "--profiles", str(profiles_path),
            "--output", str(observations_path),
            "--report", str(report_path),
            "--diagnostic-identity-probes", "1",
            "--identity-probes-report", str(probes_path),
            "--min-interval", "0",
        ],
    )

    nav.main()

    assert observations_path.read_text(encoding="utf-8") == ""
    assert "results" not in json.loads(report_path.read_text(encoding="utf-8"))
    probe_report = json.loads(probes_path.read_text(encoding="utf-8"))
    assert probe_report["results"][0]["result"] == "exact_parent_organisation_number"
    assert len(calls) == 2


@pytest.mark.parametrize("collision_target", ["profiles", "observations", "report", "websites"])
def test_cli_rejects_direct_identity_report_path_collisions(
    monkeypatch, tmp_path, collision_target
):
    profiles_path = tmp_path / "profiles.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    report_path = tmp_path / "connector-report.json"
    websites_path = tmp_path / "websites.jsonl"
    paths = {
        "profiles": profiles_path,
        "observations": observations_path,
        "report": report_path,
        "websites": websites_path,
    }
    profiles_path.write_text(json.dumps(_profile()) + "\n", encoding="utf-8")
    calls = _install_fake_transport(monkeypatch, [], {})
    argv = [
        "run_nav_jobs_connector.py",
        "--profiles", str(profiles_path),
        "--output", str(observations_path),
        "--report", str(report_path),
        "--website-candidates-output", str(websites_path),
        "--diagnostic-identity-probes", "1",
        "--identity-probes-report", str(paths[collision_target]),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc_info:
        nav.main()

    assert exc_info.value.code == 2
    assert calls == []
    assert profiles_path.read_text(encoding="utf-8") == json.dumps(_profile()) + "\n"


def test_cli_rejects_symlink_alias_for_identity_report_before_requests(monkeypatch, tmp_path):
    profiles_path = tmp_path / "profiles.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    report_path = tmp_path / "connector-report.json"
    probes_alias = tmp_path / "probe-report-alias.json"
    profiles_content = json.dumps(_profile()) + "\n"
    profiles_path.write_text(profiles_content, encoding="utf-8")
    probes_alias.symlink_to(profiles_path)
    calls = _install_fake_transport(monkeypatch, [], {})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_nav_jobs_connector.py",
            "--profiles", str(profiles_path),
            "--output", str(observations_path),
            "--report", str(report_path),
            "--diagnostic-identity-probes", "1",
            "--identity-probes-report", str(probes_alias),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        nav.main()

    assert exc_info.value.code == 2
    assert calls == []
    assert profiles_path.read_text(encoding="utf-8") == profiles_content


def test_cli_rejects_hard_link_aliases_when_filesystem_reports_same_file(monkeypatch, tmp_path):
    profiles_path = tmp_path / "profiles.jsonl"
    hard_link = tmp_path / "profiles-hard-link.jsonl"
    profiles_path.write_text(json.dumps(_profile()) + "\n", encoding="utf-8")
    try:
        os.link(profiles_path, hard_link)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    calls = _install_fake_transport(monkeypatch, [], {})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_nav_jobs_connector.py",
            "--profiles", str(profiles_path),
            "--output", str(tmp_path / "observations.jsonl"),
            "--report", str(tmp_path / "report.json"),
                "--identity-probes-report", str(hard_link),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        nav.main()

    assert exc_info.value.code == 2
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://pam-stilling-feed.nav.no:8443/api/v1/feed",
        "https://pam-stilling-feed.nav.no:444/api/v1/feed",
        "https://pam-stilling-feed.nav.no:99999/api/v1/feed",
        "https://pam-stilling-feed.nav.no:not-a-port/api/v1/feed",
        "https://pam-stilling-feed.nav.no:/api/v1/feed",
        "https://pam-stilling-feed.nav.no:0443/api/v1/feed",
    ],
)
def test_nav_transport_rejects_invalid_or_nonstandard_ports(url, monkeypatch):
    monkeypatch.setattr(
        nav.urllib.request,
        "build_opener",
        lambda *args, **kwargs: pytest.fail("unapproved URL reached the HTTP transport"),
    )
    counter = [0]

    with pytest.raises(ValueError, match="Blocked unexpected NAV API URL"):
        nav._http_get(url, {}, 1, counter)

    assert counter == [0]


@pytest.mark.parametrize(
    "url",
    [
        nav.FEED_URL,
        "https://pam-stilling-feed.nav.no:443/api/v1/feed",
    ],
)
def test_nav_transport_accepts_omitted_or_standard_https_port(url):
    assert nav._is_approved_nav_url(url) is True


@pytest.mark.parametrize(
    "destination",
    [
        "https://pam-stilling-feed.nav.no:8443/api/v1/feed",
        "https://pam-stilling-feed.nav.no:444/api/v1/feed",
        "https://pam-stilling-feed.nav.no:99999/api/v1/feed",
        "https://pam-stilling-feed.nav.no:not-a-port/api/v1/feed",
        "https://pam-stilling-feed.nav.no:/api/v1/feed",
        "https://pam-stilling-feed.nav.no:0443/api/v1/feed",
        "https://other.example/api/v1/feed",
        "http://pam-stilling-feed.nav.no/api/v1/feed",
    ],
)
def test_nav_transport_rejects_unsafe_redirect_destinations(destination):
    handler = nav._SameHostRedirectHandler([0])
    request = nav.urllib.request.Request(nav.FEED_URL)

    with pytest.raises(urllib.error.HTTPError, match="Unapproved NAV redirect"):
        handler.redirect_request(request, None, 302, "Found", {}, destination)


@pytest.mark.parametrize(
    "destination",
    [
        nav.FEED_URL,
        "https://pam-stilling-feed.nav.no:443/api/v1/feed",
    ],
)
def test_nav_transport_allows_redirects_on_standard_https_port(destination):
    counter = [0]
    handler = nav._SameHostRedirectHandler(counter)
    request = nav.urllib.request.Request(nav.FEED_URL)

    redirected = handler.redirect_request(request, None, 302, "Found", {}, destination)

    assert redirected.full_url == destination
    assert counter == [1]


@pytest.mark.parametrize(
    "failure",
    [
        http.client.IncompleteRead(b"partial", 100),
        http.client.RemoteDisconnected("peer disconnected"),
        socket.timeout("timed out"),
        ConnectionResetError("connection reset"),
        urllib.error.URLError("transport failure"),
        http.client.HTTPException("protocol failure"),
    ],
    ids=["incomplete-read", "remote-disconnect", "socket-timeout", "connection-reset", "url-error", "http-protocol"],
)
def test_probe_transport_errors_are_recorded_without_publishing(monkeypatch, failure):
    calls = _install_custom_probe_transport(monkeypatch, [_feed_item()], [failure])
    probe_report: dict = {}

    observations, websites, report = nav.run_connector(
        [_profile()],
        diagnostic_identity_probes=1,
        max_pages=1,
        max_details=2,
        min_interval=0,
        now=NOW,
        identity_probe_report_out=probe_report,
    )

    result = probe_report["results"][0]
    assert result["result"] == "detail_fetch_error"
    assert result["error_type"] == type(failure).__name__
    assert result["exact_identity_match"] is False
    assert "NAV detail request" in result["error"]
    assert observations == []
    assert websites == []
    assert report["detail_errors"] == 1
    assert report["detail_requests"] == 1
    assert report["requests_total_including_token"] == 2
    assert len(calls) == 2


def test_malformed_probe_response_is_recorded_as_a_safe_error(monkeypatch):
    calls = _install_custom_probe_transport(monkeypatch, [_feed_item()], [b"not-json"])
    probe_report: dict = {}

    observations, websites, report = nav.run_connector(
        [_profile()],
        diagnostic_identity_probes=1,
        max_pages=1,
        max_details=2,
        min_interval=0,
        now=NOW,
        identity_probe_report_out=probe_report,
    )

    assert probe_report["results"][0]["result"] == "detail_fetch_error"
    assert probe_report["results"][0]["error_type"] == "JSONDecodeError"
    assert observations == []
    assert websites == []
    assert report["detail_errors"] == 1
    assert len(calls) == 2


def test_probe_transport_error_continues_and_cli_writes_reports(monkeypatch, tmp_path):
    items = [_feed_item("ad-1"), _feed_item("ad-2")]
    calls = _install_custom_probe_transport(
        monkeypatch,
        items,
        [http.client.IncompleteRead(b"partial", 100), _detail(PARENT_ORG)],
    )
    profiles_path = tmp_path / "profiles.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    report_path = tmp_path / "connector-report.json"
    probes_path = tmp_path / "identity-probes.json"
    profiles_path.write_text(json.dumps(_profile()) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_nav_jobs_connector.py",
            "--profiles", str(profiles_path),
            "--output", str(observations_path),
            "--report", str(report_path),
            "--diagnostic-identity-probes", "2",
            "--identity-probes-report", str(probes_path),
            "--max-pages", "1",
            "--max-details", "3",
            "--min-interval", "0",
        ],
    )

    nav.main()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    probe_report = json.loads(probes_path.read_text(encoding="utf-8"))
    assert report["detail_errors"] == 1
    assert report["detail_requests"] == 2
    assert report["requests_total_including_token"] == 3
    assert [item["result"] for item in probe_report["results"]] == [
        "detail_fetch_error",
        "exact_parent_organisation_number",
    ]
    assert len(calls) == 3
    assert observations_path.read_text(encoding="utf-8") == ""


def test_near_match_retention_is_bounded_and_candidate_count_is_independent(monkeypatch):
    nav.NEAR_MATCH_RETAIN_LIMIT = 3
    rows = [
        {
            "score": score / 100,
            "sequence_ratio": score / 100,
            "token_dice": score / 100,
            "nav_ad_id": str(score),
        }
        for score in range(100)
    ]
    retained = nav._BoundedCandidateRows(nav.NEAR_MATCH_RETAIN_LIMIT)
    for row in rows:
        retained.add(row)

    assert len(retained.rows) == 3
    assert [row["score"] for row in retained.rows] == [0.99, 0.98, 0.97]

    items = [_feed_item(f"ad-{index}") for index in range(80)]
    (observations, websites, report, probe_report), _ = _run(
        monkeypatch,
        items,
        {},
    )
    near_match_review = report["near_match_review"]
    assert near_match_review["candidates_scored"] == 80
    assert near_match_review["retained_candidate_limit"] == 3
    assert len(near_match_review["top_candidates"]) == 3
    assert observations == []
    assert websites == []
    assert probe_report["results"] == []
