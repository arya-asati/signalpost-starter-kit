from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.output_contract import contract_envelope, validate_contract_envelopes
from norway_company_agent.refresh import diff_datasets, diff_profile, refresh_profile


ORG = "923609016"
SOURCE_URL = f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}"


def record(*, content: str = "a", value: str = "same", retrieved_at: str = "2026-01-01T00:00:00Z"):
    return {
        "field": "registry_live",
        "status": "available",
        "source_type": "official_registry_live",
        "source_class": "official_registry_live",
        "source_url": SOURCE_URL,
        "retrieved_at": retrieved_at,
        "content_sha256": content * 64,
        "value": {"organisation_number": ORG, "name": value},
    }


def profile(evidence_records: dict[str, dict]) -> dict:
    registry = evidence_records.get("registry_live") or record()
    return {
        "organisation_number": ORG,
        "name": (registry.get("value") or {}).get("name"),
        "evidence": evidence_records,
    }


def test_identical_evidence_is_idempotent_even_when_retrieved_again_later():
    old = profile({"registry_live": record(retrieved_at="2026-01-01T00:00:00Z")})
    current = profile({"registry_live": record(retrieved_at="2026-01-02T00:00:00Z")})

    refreshed = refresh_profile(old, current, observed_modules={"registry_live"})
    repeated = refresh_profile(refreshed, current, observed_modules={"registry_live"})

    assert refreshed["changes"] == []
    assert refreshed["refresh_metadata"]["idempotent"] is True
    assert len(refreshed["historical_evidence"]) == 1
    assert repeated["changes"] == []
    assert repeated["refresh_metadata"]["idempotent"] is True
    assert repeated["historical_evidence"] == refreshed["historical_evidence"]


@pytest.mark.parametrize(
    ("old_records", "new_records", "expected"),
    [
        ({}, {"roles": record()}, "added"),
        ({"roles": record()}, {"roles": record(content="b", value="changed")}, "changed"),
        ({"roles": record()}, {}, "removed"),
    ],
)
def test_refresh_reports_added_changed_and_removed_evidence(old_records, new_records, expected):
    changes = diff_profile(
        profile(old_records),
        profile(new_records),
        observed_modules={"roles"},
    )

    event = next(item for item in changes if item["field"] == "evidence.roles")
    assert event["status"] == expected
    assert event["organisation_number"] == ORG


def test_refresh_preserves_old_evidence_as_historical_not_current():
    old = profile({"registry_live": record(content="a", value="old")})
    current = profile({"registry_live": record(content="b", value="new")})
    refreshed = refresh_profile(old, current, observed_modules={"registry_live"})

    assert refreshed["evidence"]["registry_live"]["value"]["name"] == "new"
    assert refreshed["historical_evidence"][0]["temporal_status"] == "historical"
    assert refreshed["historical_evidence"][0]["record"]["value"]["name"] == "old"
    envelope = contract_envelope(
        refreshed,
        run_id="refresh-test",
        modules=["registry_live"],
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-02T00:00:00Z",
    )
    assert envelope["refresh_metadata"]["status"] == "checked"
    assert envelope["historical_evidence"][0]["temporal_status"] == "historical"
    assert any(
        item.get("temporal_status") == "historical"
        and item.get("content_sha256") == "a" * 64
        for item in envelope["source_snapshots"]
    )
    assert all(
        not reference.startswith("hist-")
        for claim in envelope["claims"]
        for reference in claim["evidence_ids"]
    )
    assert validate_contract_envelopes([envelope], 1)["passed"]


def test_refresh_rejects_changed_organisation_identity():
    old = profile({"registry_live": record()})
    current = copy.deepcopy(old)
    current["organisation_number"] = "999999999"
    with pytest.raises(ValueError, match="same exact organisation number"):
        refresh_profile(old, current)


def test_refresh_dataset_rejects_duplicate_company_identity():
    old = profile({"registry_live": record()})
    with pytest.raises(ValueError, match="unique exact organisation numbers"):
        diff_datasets([old, copy.deepcopy(old)], [old])
