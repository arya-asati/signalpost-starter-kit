from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .official import ACCOUNTING_OBLIGATION_SOURCE, accounting_obligation_assessment
from .sampling import iter_bulk


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}


ORG_NUMBER_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)


def normalize_organisation_number(value: Any) -> str:
    """Normalize and validate a Norwegian nine-digit organisation number."""
    raw = str(value or "").strip()
    org = re.sub(r"[\s-]", "", raw)
    if not re.fullmatch(r"[0-9]{9}", org):
        raise ValueError(f"Invalid Norwegian organisation number: {value!r}")
    weighted_sum = sum(
        int(digit) * weight
        for digit, weight in zip(org[:8], ORG_NUMBER_WEIGHTS)
    )
    check_digit = 11 - (weighted_sum % 11)
    if check_digit == 11:
        check_digit = 0
    if check_digit == 10 or check_digit != int(org[-1]):
        raise ValueError(f"Invalid Norwegian organisation number checksum: {value!r}")
    return org


def read_organisation_inputs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    values: list[Any]
    if source.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    elif source.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    records = []
    for value in values:
        org_value = value.get("organisation_number") if isinstance(value, dict) else value
        org = normalize_organisation_number(org_value)
        record = {"organisation_number": org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    orgs = [record["organisation_number"] for record in records]
    if len(orgs) != len(set(orgs)):
        raise ValueError("Organisation-number input contains duplicates")
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested = list(organisation_numbers)
    wanted = set(requested)
    snapshot_hasher = hashlib.sha256()
    with Path(path).open("rb") as snapshot_file:
        for chunk in iter(lambda: snapshot_file.read(1024 * 1024), b""):
            snapshot_hasher.update(chunk)
    snapshot_sha256 = snapshot_hasher.hexdigest()
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = profile.pop("raw", {})
        source_row_text = raw.pop(
    "__signalpost_source_row_text__",
    None,
)
        registry_record = {
            **evidence(
                "registry",
                "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=raw,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
            ),
            **(
                {"claim_span": source_row_text}
                if source_row_text
                else {}
            ),
        }
        accounting_record = accounting_obligation_assessment(profile)
        # This is a local interpretation of the exact registry row. Keep the
        # registry source URL/hash alongside the separate ruleset hash so the
        # resulting claims retain both data and logic provenance.
        accounting_record.update({
            "source_url": registry_record["source_url"],
            "retrieved_at": registry_record["retrieved_at"],
            "content_sha256": registry_record["content_sha256"],
            "source_row_key": org,
            "reference_url": ACCOUNTING_OBLIGATION_SOURCE,
        })
        profile["evidence"] = {
            "registry": registry_record,
            "accounting_obligation": accounting_record,
        }
        found[org] = profile
        if len(found) == len(wanted):
            break
    missing = [org for org in requested if org not in found]
    for org in missing:
        found[org] = {
            "organisation_number": org,
            "name": None,
            "evidence": {
                "registry": evidence(
                    "registry",
                    "source_error",
                    "agent_execution",
                    "",
                    value=None,
                    retrieved_at=retrieved_at,
                    note=(
                        "Organisation number is absent from the supplied "
                        "frozen registry snapshot; no live fallback is "
                        "configured for this run."
                    ),
                ),
            },
            "errors": [{
                "kind": "registry_snapshot_missing",
                "message": "No profile was found in the supplied registry snapshot.",
            }],
        }
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found) - len(missing),
        "snapshot_missing": len(missing),
        "missing_organisation_numbers": missing,
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status == "available":
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status == "not_found":
        return "not_found"
    if status == "blocked":
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status == "source_error":
        return "source_error"
    if status == "budget_exhausted":
        return "budget_exhausted"
    return "submission_error"


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"
    return {
        "run_id": run_id,
        "organisation_number": profile["organisation_number"],
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "modules": module_states,
        "profile": profile,
    }


def validate_envelopes(
    envelopes: list[dict[str, Any]],
    expected_count: int,
    expected_organisation_numbers: Iterable[str] | None = None,
) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    expected_orgs = (
        list(expected_organisation_numbers)
        if expected_organisation_numbers is not None
        else None
    )
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "input_membership_and_order_match": (
            expected_orgs is None or orgs == expected_orgs
        ),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": (
            len(envelopes) == expected_count
            and len(orgs) == len(set(orgs))
            and (expected_orgs is None or orgs == expected_orgs)
        ),
    }
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states}


def profile_complete_for_modules(
    profile: dict[str, Any],
    modules: Iterable[str],
) -> bool:
    """Return False for incomplete modules or unexpected worker failures."""

    errors = profile.get("errors") or []

    # An unexpected worker exception should be retried on resume.
    if any(
        isinstance(error, dict)
        and error.get("kind") == "worker_exception"
        for error in errors
    ):
        return False

    records = profile.get("evidence", {})

    return all(
        module in records
        and records[module].get("status") != "not_fetched"
        for module in modules
    )
