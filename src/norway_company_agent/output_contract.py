from __future__ import annotations

import hashlib
from typing import Any, Iterable


AVAILABILITY_STATES = {
    "available",
    "not_available",
    "blocked",
    "not_applicable",
    "ambiguous",
    "failed",
}

STATUS_MAP = {
    "available": "available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "blocked": "blocked",
    "source_error": "failed",
    "not_fetched": "failed",
}

FREE_MODULES = {
    "registry",
    "accounting_obligation",
    "registry_live",
    "financials",
    "financial_history",
    "roles",
    "group",
    "locations",
    "website",
}

INTERNAL_VALUE_KEYS = {
    "content_sha256",
    "identity_assessment",
    "social_link_assessments",
    "discovered_social_links",
    "pages",
    "crawl_errors",
    "main_text_excerpt",
    "identity_text_excerpt",
    "js_fallback",
    "extraction_state",
    "requested_url",
    "registered_domain",
}


def availability_from_evidence(
    status: str | None,
    value: Any = None,
) -> str:
    """Map internal evidence statuses to contract availability states."""

    # Preserve official states that are already supplied.
    if status in AVAILABILITY_STATES and status != "available":
        return status

    # Map the starter's internal statuses to official states.
    if status != "available":
        return STATUS_MAP.get(status or "", "failed")

    # An empty list can be a successful check with zero results.
    if isinstance(value, list):
        return "available"

    # Missing values are not converted into zero.
    if value is None:
        return "not_available"

    if isinstance(value, str) and not value.strip():
        return "not_available"

    if isinstance(value, dict) and not value:
        return "not_available"

    # Preserve meaningful values such as 0 and False.
    return "available"


def _evidence_id(
    organisation_number: str,
    module: str,
    record: dict[str, Any],
) -> str:
    identity = "|".join(
        (
            organisation_number,
            module,
            str(record.get("source_url") or ""),
            str(record.get("content_sha256") or ""),
        )
    )
    return "ev-" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:16]


def _evidence_item(
    evidence_id: str,
    record: dict[str, Any],
    module: str,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": evidence_id,
        "source_class": (
            record.get("source_class")
            or record.get("source_type")
            or "unknown"
        ),
    }

    source_url = record.get("source_url")
    retrieved_at = record.get("retrieved_at")
    content_hash = record.get("content_sha256")

    if source_url:
        item["source_url"] = source_url

    if retrieved_at:
        item["retrieved_at"] = retrieved_at

    if content_hash:
        item["content_sha256"] = content_hash

    if record.get("claim_span"):
        item["claim_span"] = record["claim_span"]

    if record.get("note"):
        item["note"] = record["note"]

    return item


def _flatten(
    prefix: str,
    value: Any,
) -> list[tuple[str, Any]]:
    """Turn structured source values into individual claim fields."""

    if isinstance(value, dict):
        visible = {
            key: child
            for key, child in value.items()
            if key not in INTERNAL_VALUE_KEYS
        }

        if not visible:
            return [(prefix, value)] if not value else []

        result = []

        for key, child in visible.items():
            result.extend(
                _flatten(f"{prefix}.{key}", child)
            )

        return result

    if isinstance(value, list):
        if not value:
            return [(prefix, [])]

        result = []

        for index, child in enumerate(value):
            result.extend(
                _flatten(f"{prefix}[{index}]", child)
            )

        return result

    return [(prefix, value)]


def _claim(
    field: str,
    value: Any,
    availability: str,
    evidence_ids: list[str],
) -> dict[str, Any]:
    return {
        "field": field,
        "value": value,
        "availability": availability,
        "evidence_ids": evidence_ids,
    }


def _claims_for_record(
    organisation_number: str,
    module: str,
    record: dict[str, Any],
    profile: dict[str, Any],
    evidence_id: str,
) -> list[dict[str, Any]]:
    status = record.get("status")
    evidence_ids = [evidence_id]

    if module == "website":
        value = record.get("value") or {}
        assessment = value.get("identity_assessment") or {}

        website_url = (
            value.get("final_url")
            or record.get("source_url")
            or profile.get("website")
        )

        if status != "available":
            return [
                _claim(
                    "official_website",
                    None,
                    availability_from_evidence(status),
                    evidence_ids,
                )
            ]

        # Do not publish website facts as verified until identity
        # has passed the existing exact-company identity gate.
        if assessment.get("publishable") is not True:
            availability = (
                "ambiguous"
                if website_url
                else "not_available"
            )

            return [
                _claim(
                    "official_website",
                    website_url,
                    availability,
                    evidence_ids,
                )
            ]

        result = [
            _claim(
                "official_website",
                website_url,
                availability_from_evidence(
                    status,
                    website_url,
                ),
                evidence_ids,
            )
        ]

        for key in ("title", "description"):
            if key in value:
                result.append(
                    _claim(
                        f"website.{key}",
                        value[key],
                        availability_from_evidence(
                            status,
                            value[key],
                        ),
                        evidence_ids,
                    )
                )

        for field, child in _flatten(
            "website.social_links",
            value.get("social_links") or [],
        ):
            result.append(
                _claim(
                    field,
                    child,
                    availability_from_evidence(status, child),
                    evidence_ids,
                )
            )

        return result

    if status != "available":
        return [
            _claim(
                module,
                None,
                availability_from_evidence(status),
                evidence_ids,
            )
        ]

    if module == "registry":
        keys = (
            "organisation_number",
            "name",
            "legal_form",
            "industry",
            "employees",
            "business_address",
            "postal_address",
            "municipality",
            "website",
            "latest_submitted_accounts",
        )

        value = {
            key: profile[key]
            for key in keys
            if key in profile
        }

        if not value:
            value = record.get("value")
    else:
        value = record.get("value")

    leaves = _flatten(module, value)

    if not leaves:
        return [
            _claim(
                module,
                None,
                "not_available",
                evidence_ids,
            )
        ]

    return [
        _claim(
            field,
            child,
            availability_from_evidence(status, child),
            evidence_ids,
        )
        for field, child in leaves
    ]


def contract_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    """Build a contract-shaped result without replacing the source profile."""

    organisation_number = str(
        profile.get("organisation_number") or ""
    )

    evidence_records = profile.get("evidence") or {}

    requested_modules = list(dict.fromkeys(modules))

    module_names = list(
        dict.fromkeys(
            requested_modules + list(evidence_records)
        )
    )

    evidence_items = []
    claims = []
    errors = []
    seen_evidence_ids = set()

    for module in module_names:
        record = evidence_records.get(module)

        if not record:
            # This records a missing module as an internal execution
            # event. It does not invent a website, source URL, or hash.
            record = {
                "field": module,
                "status": "not_fetched",
                "source_type": "agent_execution",
                "source_class": "agent_execution",
                "retrieved_at": completed_at,
                "note": (
                    "No evidence record was produced for this module. "
                    "This is an execution-status record, not external "
                    "source evidence."
                ),
            }

            evidence_id = _evidence_id(
                organisation_number,
                module,
                record,
            )

            if evidence_id not in seen_evidence_ids:
                evidence_items.append(
                    _evidence_item(
                        evidence_id,
                        record,
                        module,
                    )
                )
                seen_evidence_ids.add(evidence_id)

            claims.append(
                _claim(
                    module,
                    None,
                    "failed",
                    [evidence_id],
                )
            )

            errors.append(
                {
                    "field": module,
                    "status": "failed",
                    "message": (
                        "No evidence record was produced for this module."
                    ),
                }
            )

            continue

        evidence_id = _evidence_id(
            organisation_number,
            module,
            record,
        )

        if evidence_id not in seen_evidence_ids:
            evidence_items.append(
                _evidence_item(
                    evidence_id,
                    record,
                    module,
                )
            )
            seen_evidence_ids.add(evidence_id)

        claims.extend(
            _claims_for_record(
                organisation_number,
                module,
                record,
                profile,
                evidence_id,
            )
        )

        if record.get("status") in {
            "source_error",
            "not_fetched",
        }:
            errors.append(
                {
                    "field": module,
                    "status": record["status"],
                    "message": (
                        record.get("note")
                        or "Source could not be checked."
                    ),
                    "source_url": record.get("source_url"),
                }
            )

    metrics = profile.get("run_metrics") or {}

    cost = metrics.get("third_party_cost_usd")

    if (
        cost is None
        and set(module_names).issubset(FREE_MODULES)
    ):
        cost = 0.0

    legal_identity = {
        "organisation_number": organisation_number,
    }

    for key in (
        "name",
        "legal_form",
        "public_brand",
        "industry",
    ):
        if profile.get(key) is not None:
            legal_identity[key] = profile[key]

    source_snapshots = []
    seen_snapshots = set()

    for item in evidence_items:
        snapshot = {
            key: item[key]
            for key in (
                "source_url",
                "retrieved_at",
                "content_sha256",
            )
            if item.get(key)
        }

        snapshot_key = tuple(snapshot.items())

        if snapshot and snapshot_key not in seen_snapshots:
            source_snapshots.append(snapshot)
            seen_snapshots.add(snapshot_key)

    refresh_checked = bool(
        profile.get("refresh_checked")
    )

    return {
        "organisation_number": organisation_number,
        "legal_identity": legal_identity,
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": "completed",
        },
        "claims": claims,
        "evidence": evidence_items,
        "source_snapshots": source_snapshots,
        "refresh_metadata": {
            "status": (
                "checked"
                if refresh_checked
                else "not_checked"
            ),
            "previous_snapshot_available": bool(
                profile.get("previous_snapshot_available", False)
            ),
        },
        "changes": profile.get("changes") or [],
        "errors": errors + list(profile.get("errors") or []),
        "operations": {
            "requests": metrics.get("requests"),
            "runtime_ms": metrics.get("runtime_ms"),
            "third_party_cost_usd": cost,
        },
    }
def validate_contract_envelopes(
    envelopes: list[dict[str, Any]],
    expected_count: int,
) -> dict[str, Any]:
    """Validate the contract-shaped envelopes before writing output."""

    organisation_numbers = [
        item.get("organisation_number")
        for item in envelopes
    ]

    invalid_states = []
    dangling_evidence_refs = []
    missing_sections = []
    identity_errors = []

    required_sections = {
        "organisation_number",
        "legal_identity",
        "run",
        "claims",
        "evidence",
        "source_snapshots",
        "refresh_metadata",
        "changes",
        "errors",
        "operations",
    }

    for envelope_index, envelope in enumerate(envelopes):
        missing = sorted(required_sections - set(envelope))

        if missing:
            missing_sections.append({
                "index": envelope_index,
                "missing": missing,
            })

        org = envelope.get("organisation_number")
        legal_identity = envelope.get("legal_identity") or {}

        if (
            not org
            or legal_identity.get("organisation_number") != org
        ):
            identity_errors.append({
                "index": envelope_index,
                "organisation_number": org,
            })

        evidence_rows = envelope.get("evidence") or []
        evidence_ids = [
            item.get("id")
            for item in evidence_rows
            if item.get("id")
        ]
        evidence_id_set = set(evidence_ids)

        claims = envelope.get("claims") or []

        for claim_index, claim in enumerate(claims):
            state = claim.get("availability")

            if state not in AVAILABILITY_STATES:
                invalid_states.append({
                    "organisation_number": org,
                    "claim_index": claim_index,
                    "state": state,
                })

            refs = claim.get("evidence_ids")

            if not isinstance(refs, list) or not refs:
                dangling_evidence_refs.append({
                    "organisation_number": org,
                    "claim_index": claim_index,
                    "reason": "missing evidence references",
                })
                continue

            for evidence_id in refs:
                if evidence_id not in evidence_id_set:
                    dangling_evidence_refs.append({
                        "organisation_number": org,
                        "claim_index": claim_index,
                        "evidence_id": evidence_id,
                        "reason": "evidence reference not found",
                    })

    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": (
            len(organisation_numbers)
            == len(set(organisation_numbers))
        ),
        "required_sections_present": not missing_sections,
        "legal_identity_matches_envelope": not identity_errors,
        "all_claim_states_allowed": not invalid_states,
        "all_claim_evidence_references_resolve": (
            not dangling_evidence_refs
        ),
    }

    return {
        "passed": all(checks.values()),
        "checks": checks,
        "invalid_states": invalid_states,
        "dangling_evidence_refs": dangling_evidence_refs,
        "missing_sections": missing_sections,
        "identity_errors": identity_errors,
    }