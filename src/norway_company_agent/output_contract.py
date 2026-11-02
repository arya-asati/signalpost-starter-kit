
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlsplit


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
    "budget_exhausted": "failed",
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

SOURCE_RIGHTS_STATUS = {
    "official_registry_bulk": "open_data_nlod_2_0",
    "official_registry_live": "open_data_nlod_2_0",
    "official_annual_accounts": "open_data_nlod_2_0",
    "official_annual_account_copies": "open_data_nlod_2_0",
    "official_roles": "open_data_nlod_2_0",
    "official_group_structure": "open_data_nlod_2_0",
    "official_subunits": "open_data_nlod_2_0",
    "registry_linked_company_website": "company_owned_factual_use",
    "local_ruleset_interpretation": "local_ruleset_derivative",
    "agent_execution": "not_applicable",
}


def _valid_source_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and (
            port is None
            or port == (443 if parsed.scheme == "https" else 80)
        )
    )


def _timestamp_is_valid(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _org_number_in_url(url: str, organisation_number: str) -> bool:
    try:
        parsed = urlsplit(url)
        path_parts = [unquote(part) for part in parsed.path.split("/") if part]
        query_values = [
            item
            for values in parse_qs(parsed.query, keep_blank_values=True).values()
            for item in values
        ]
    except ValueError:
        return False
    return organisation_number in path_parts or organisation_number in query_values


def _identity_proof(
    organisation_number: str,
    module: str,
    record: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any] | None:
    source_class = record.get("source_class") or record.get("source_type")
    source_url = str(record.get("source_url") or "")
    value = record.get("value") or {}

    row_key = record.get("source_row_key")
    if row_key not in (None, "", organisation_number):
        return None

    if source_class in SOURCE_RIGHTS_STATUS and source_class.startswith("official_"):
        try:
            parsed = urlsplit(source_url)
            host = (parsed.hostname or "").casefold()
            path = unquote(parsed.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
        except ValueError:
            return None
        if host != "data.brreg.no":
            return None
        exact_row = row_key == organisation_number
        exact_value = (
            isinstance(value, dict)
            and str(value.get("organisation_number") or "") == organisation_number
        )
        expected_paths = {
            "official_registry_bulk": "/enhetsregisteret/api/enheter/lastned/csv",
            "official_registry_live": f"/enhetsregisteret/api/enheter/{organisation_number}",
            "official_annual_accounts": f"/regnskapsregisteret/regnskap/{organisation_number}",
            "official_annual_account_copies": f"/regnskapsregisteret/regnskap/aarsregnskap/kopi/{organisation_number}/aar",
            "official_roles": f"/enhetsregisteret/api/enheter/{organisation_number}/roller",
            "official_group_structure": f"/enhetsregisteret/api/konsernstruktur/{organisation_number}",
            "official_subunits": "/enhetsregisteret/api/underenheter",
        }
        exact_endpoint = path == expected_paths.get(source_class)
        if source_class == "official_subunits":
            exact_endpoint = (
                exact_endpoint
                and query.get("overordnetEnhet") == [organisation_number]
            )
        elif source_class == "official_registry_bulk":
            exact_endpoint = exact_endpoint and bool(exact_row or exact_value)

        if exact_endpoint:
            return {
                "method": (
                    "exact_registry_row_key"
                    if exact_row
                    else "exact_source_entity_number"
                    if exact_value
                    else "exact_official_endpoint_organisation_number"
                ),
                "organisation_number": organisation_number,
                "verified": True,
            }
        return None

    if source_class == "local_ruleset_interpretation":
        registry = (profile.get("evidence") or {}).get("registry") or {}
        if (
            row_key == organisation_number
            and record.get("ruleset_sha256")
            and registry.get("source_row_key") == organisation_number
            and registry.get("content_sha256") == record.get("content_sha256")
        ):
            return {
                "method": "exact_registry_row_and_local_ruleset",
                "organisation_number": organisation_number,
                "verified": True,
            }
        return None

    if record.get("source_row_key") == organisation_number:
        return {
            "method": "exact_registry_row_key",
            "organisation_number": organisation_number,
            "verified": True,
        }

    if isinstance(value, dict) and str(value.get("organisation_number") or "") == organisation_number:
        return {
            "method": "exact_source_entity_number",
            "organisation_number": organisation_number,
            "verified": True,
        }

    if source_class == "registry_linked_company_website":
        assessment = value.get("identity_assessment") or {}
        if (
            assessment.get("status") == "exact"
            and assessment.get("publishable") is True
            and assessment.get("method") == "deterministic_name_org_evidence_v4"
        ):
            return {
                "method": assessment["method"],
                "organisation_number": organisation_number,
                "verified": True,
            }
        return None

    return None


def _identity_proof_is_valid(item: dict[str, Any], organisation_number: str) -> bool:
    proof = item.get("identity_proof") or {}
    if (
        proof.get("verified") is not True
        or proof.get("organisation_number") != organisation_number
        or item.get("organisation_number") != organisation_number
    ):
        return False

    source_class = item.get("source_class")
    source_url = str(item.get("source_url") or "")
    method = proof.get("method")
    if source_class in SOURCE_RIGHTS_STATUS and source_class.startswith("official_"):
        try:
            parsed = urlsplit(source_url)
            host = (parsed.hostname or "").casefold()
            path = unquote(parsed.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
        except ValueError:
            return False
        expected_paths = {
            "official_registry_bulk": "/enhetsregisteret/api/enheter/lastned/csv",
            "official_registry_live": f"/enhetsregisteret/api/enheter/{organisation_number}",
            "official_annual_accounts": f"/regnskapsregisteret/regnskap/{organisation_number}",
            "official_annual_account_copies": f"/regnskapsregisteret/regnskap/aarsregnskap/kopi/{organisation_number}/aar",
            "official_roles": f"/enhetsregisteret/api/enheter/{organisation_number}/roller",
            "official_group_structure": f"/enhetsregisteret/api/konsernstruktur/{organisation_number}",
            "official_subunits": "/enhetsregisteret/api/underenheter",
        }
        if host != "data.brreg.no" or path != expected_paths.get(source_class):
            return False
        if source_class == "official_registry_bulk":
            return (
                item.get("source_row_key") == organisation_number
                or method == "exact_source_entity_number"
            )
        if source_class == "official_subunits":
            return query.get("overordnetEnhet") == [organisation_number]
        return method in {
            "exact_source_entity_number",
            "exact_official_endpoint_organisation_number",
        }

    if source_class == "local_ruleset_interpretation":
        return (
            item.get("source_row_key") == organisation_number
            and bool(item.get("ruleset_sha256"))
            and method == "exact_registry_row_and_local_ruleset"
        )

    if source_class == "registry_linked_company_website":
        return (
            item.get("identity_assessment_status") == "exact"
            and item.get("identity_assessment_method") == "deterministic_name_org_evidence_v4"
            and method == "deterministic_name_org_evidence_v4"
        )

    return False


def _reporting_periods(module: str, record: dict[str, Any]) -> list[Any]:
    if module != "financials":
        return []
    value = record.get("value") or {}
    rows = value.get("records") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        return []
    periods = []
    for row in rows:
        if isinstance(row, dict) and row.get("period") not in (None, "", {}, []):
            periods.append(row["period"])
    return periods


def availability_from_evidence(
    status: str | None,
    value: Any = None,
) -> str:
    """Map internal evidence statuses to contract availability states."""

    if status in AVAILABILITY_STATES and status != "available":
        return status

    if status != "available":
        return STATUS_MAP.get(status or "", "failed")

    # A successfully checked source can legitimately return no results.
    if isinstance(value, list):
        return "available"

    if value is None:
        return "not_available"

    if isinstance(value, str) and not value.strip():
        return "not_available"

    if isinstance(value, dict) and not value:
        return "not_available"

    # Preserve meaningful values such as zero and False.
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
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return "ev-" + digest[:16]


def _evidence_item(
    evidence_id: str,
    record: dict[str, Any],
    module: str,
    organisation_number: str,
    profile: dict[str, Any],
    *,
    temporal_status: str = "current",
) -> dict[str, Any]:
    source_class = (
        record.get("source_class")
        or record.get("source_type")
        or "unknown"
    )
    item: dict[str, Any] = {
        "id": evidence_id,
        "source_class": source_class,
        "status": record.get("status"),
        "organisation_number": organisation_number,
        "identity_proof": _identity_proof(
            organisation_number,
            module,
            record,
            profile,
        ),
        "rights_status": record.get("rights_status") or SOURCE_RIGHTS_STATUS.get(source_class),
        "temporal_status": temporal_status,
    }
    if source_class == "registry_linked_company_website":
        assessment = (record.get("value") or {}).get("identity_assessment") or {}
        item["identity_assessment_status"] = assessment.get("status")
        item["identity_assessment_method"] = assessment.get("method")

    for key in (
        "source_url",
        "reference_url",
        "retrieved_at",
        "content_sha256",
        "source_row_key",
        "ruleset_sha256",
        "as_of",
        "effective_at",
        "claim_span",
        "note",
        "reference_url",
        "ruleset_sha256",
    ):
        value = record.get(key)
        if value is not None and value != "":
            item[key] = value

    reporting_periods = _reporting_periods(module, record)
    if reporting_periods:
        item["reporting_periods"] = reporting_periods

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

        result: list[tuple[str, Any]] = []

        for key, child in visible.items():
            result.extend(_flatten(f"{prefix}.{key}", child))

        return result

    if isinstance(value, list):
        if not value:
            return [(prefix, [])]

        result: list[tuple[str, Any]] = []

        for index, child in enumerate(value):
            result.extend(_flatten(f"{prefix}[{index}]", child))

        return result

    return [(prefix, value)]


def _claim(
    field: str,
    value: Any,
    availability: str,
    evidence_ids: list[str],
    confidence: float | None = None,
) -> dict[str, Any]:
    """Build a claim without converting missing values into facts."""

    if availability == "not_available":
        value = None

    return {
        "field": field,
        "value": value,
        "availability": availability,
        "confidence": confidence,
        "evidence_ids": evidence_ids,
    }


def _confidence_for_claim(
    claim: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> float:
    """
    Return an explicit provenance heuristic, not a calibrated probability.

    These values have not been statistically calibrated against independently
    labelled claims. Do not describe them as calibrated probabilities.
    """

    availability = claim.get("availability")
    value = claim.get("value")

    if availability == "ambiguous":
        return 0.40 if value is not None else 0.0

    if availability != "available" or value is None:
        return 0.0

    source_scores = {
        "official_registry_bulk": 0.95,
        "official_registry_live": 0.95,
        "official_annual_accounts": 0.95,
        "official_annual_account_copies": 0.90,
        "official_roles": 0.90,
        "official_group_structure": 0.90,
        "official_subunits": 0.90,
        "registry_linked_company_website": 0.80,
        "independently_verified_company_website": 0.80,
        "local_ruleset_interpretation": 0.75,
        # Support older evidence records during migration.
        "official_rule_interpretation": 0.75,
    }

    referenced = [
        evidence_by_id[evidence_id]
        for evidence_id in claim.get("evidence_ids", [])
        if evidence_id in evidence_by_id
    ]

    if not referenced:
        return 0.0

    scores: list[float] = []

    for item in referenced:
        source_class = item.get("source_class") or "unknown"
        score = source_scores.get(source_class, 0.50)

        # An external claim without a usable excerpt gets a conservative cap.
        if (
            item.get("source_url")
            and source_class != "local_ruleset_interpretation"
            and source_class != "official_rule_interpretation"
            and not str(item.get("claim_span") or "").strip()
        ):
            score = min(score, 0.65)

        scores.append(score)

    # If multiple sources support a claim, use the lowest provenance score.
    return min(scores)


def _claims_for_record(
    organisation_number: str,
    module: str,
    record: dict[str, Any],
    profile: dict[str, Any],
    evidence_id: str,
) -> list[dict[str, Any]]:
    status = record.get("status")
    evidence_ids = [evidence_id]

    # Accounting classification depends on local rules and registry inputs.
    # Link both evidence records, rather than attributing all facts solely
    # to the ruleset interpretation.
    if module == "accounting_obligation":
        registry_record = (profile.get("evidence") or {}).get("registry")

        if isinstance(registry_record, dict):
            registry_evidence_id = _evidence_id(
                organisation_number,
                "registry",
                registry_record,
            )
            evidence_ids = list(
                dict.fromkeys([registry_evidence_id, evidence_id])
            )

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

        # Do not publish website facts as verified until identity has passed
        # the existing exact-company identity gate.
        if (
            assessment.get("status") != "exact"
            or assessment.get("publishable") is not True
            or assessment.get("method") != "deterministic_name_org_evidence_v4"
        ):
            availability = (
                "ambiguous" if website_url else "not_available"
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
                availability_from_evidence(status, website_url),
                evidence_ids,
            )
        ]

        for key in ("title", "description"):
            if key in value:
                result.append(
                    _claim(
                        f"website.{key}",
                        value[key],
                        availability_from_evidence(status, value[key]),
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
        dict.fromkeys(requested_modules + list(evidence_records))
    )

    evidence_items: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_evidence_ids: set[str] = set()

    for module in module_names:
        record = evidence_records.get(module)

        if not record:
            # Internal execution status, not invented external evidence.
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
                        organisation_number,
                        profile,
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
                    organisation_number,
                    profile,
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
            "budget_exhausted",
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

    if cost is None and set(module_names).issubset(FREE_MODULES):
        cost = 0.0

    legal_identity = {"organisation_number": organisation_number}

    for key in ("name", "legal_form", "public_brand", "industry"):
        if profile.get(key) is not None:
            legal_identity[key] = profile[key]

    historical_items = []
    historical_ids: set[str] = set()
    for historical in profile.get("historical_evidence") or []:
        if not isinstance(historical, dict):
            continue
        module = str(historical.get("module") or "unknown")
        record = historical.get("record")
        if not isinstance(record, dict):
            continue
        stable = json.dumps(
            {
                "organisation_number": organisation_number,
                "module": module,
                "record": record,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        history_id = "hist-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]
        if history_id in historical_ids:
            continue
        historical_ids.add(history_id)
        item = _evidence_item(
            history_id,
            record,
            module,
            organisation_number,
            profile,
            temporal_status="historical",
        )
        item["module"] = module
        item["record"] = record
        historical_items.append(item)

    source_snapshots = []
    seen_snapshots = set()

    for item in evidence_items:
        snapshot = {
            key: item[key]
            for key in (
                "source_url",
                "retrieved_at",
                "content_sha256",
                "organisation_number",
                "temporal_status",
            )
            if item.get(key)
        }

        snapshot_key = tuple(snapshot.items())

        if snapshot and snapshot_key not in seen_snapshots:
            source_snapshots.append(snapshot)
            seen_snapshots.add(snapshot_key)

    for item in historical_items:
        snapshot = {
            key: item[key]
            for key in (
                "source_url",
                "retrieved_at",
                "content_sha256",
                "organisation_number",
                "temporal_status",
            )
            if item.get(key)
        }
        snapshot_key = tuple(snapshot.items())
        if snapshot and snapshot_key not in seen_snapshots:
            source_snapshots.append(snapshot)
            seen_snapshots.add(snapshot_key)

    # Calculate transparent, heuristic confidence after the full evidence
    # set has been assembled so references can be resolved.
    evidence_by_id = {
        item["id"]: item
        for item in evidence_items
        if item.get("id")
    }

    for claim in claims:
        if claim.get("confidence") is None:
            claim["confidence"] = _confidence_for_claim(
                claim,
                evidence_by_id,
            )

    refresh_checked = bool(profile.get("refresh_checked"))
    refresh_metadata = profile.get("refresh_metadata") or {
        "status": "checked" if refresh_checked else "not_checked",
        "previous_snapshot_available": bool(
            profile.get("previous_snapshot_available", False)
        ),
    }

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
        "historical_evidence": historical_items,
        "source_snapshots": source_snapshots,
        "refresh_metadata": refresh_metadata,
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
    provenance_errors = []
    historical_reference_errors = []
    refresh_errors = []

    required_sections = {
        "organisation_number",
        "legal_identity",
        "run",
        "claims",
        "evidence",
        "historical_evidence",
        "source_snapshots",
        "refresh_metadata",
        "changes",
        "errors",
        "operations",
    }

    for envelope_index, envelope in enumerate(envelopes):
        missing = sorted(required_sections - set(envelope))

        if missing:
            missing_sections.append(
                {
                    "index": envelope_index,
                    "missing": missing,
                }
            )

        org = envelope.get("organisation_number")
        legal_identity = envelope.get("legal_identity") or {}

        if (
            not org
            or legal_identity.get("organisation_number") != org
        ):
            identity_errors.append(
                {
                    "index": envelope_index,
                    "organisation_number": org,
                }
            )

        refresh_metadata = envelope.get("refresh_metadata") or {}
        refresh_status = refresh_metadata.get("status")
        if refresh_status not in {"not_checked", "initialized", "checked"}:
            refresh_errors.append({
                "index": envelope_index,
                "organisation_number": org,
                "reason": "unknown refresh status",
            })
        elif refresh_status == "checked":
            if (
                refresh_metadata.get("previous_snapshot_available") is not True
                or not re.fullmatch(
                    r"[0-9a-fA-F]{64}",
                    str(refresh_metadata.get("previous_snapshot_sha256") or ""),
                )
                or not re.fullmatch(
                    r"[0-9a-fA-F]{64}",
                    str(refresh_metadata.get("current_snapshot_sha256") or ""),
                )
                or not isinstance(refresh_metadata.get("idempotent"), bool)
            ):
                refresh_errors.append({
                    "index": envelope_index,
                    "organisation_number": org,
                    "reason": "checked refresh metadata is incomplete",
                })
        elif refresh_status == "initialized" and refresh_metadata.get("previous_snapshot_available") is not False:
            refresh_errors.append({
                "index": envelope_index,
                "organisation_number": org,
                "reason": "initial refresh cannot claim a previous snapshot",
            })

        evidence_rows = envelope.get("evidence") or []
        evidence_by_id = {
            item.get("id"): item
            for item in evidence_rows
            if item.get("id")
        }
        evidence_ids = set(evidence_by_id)
        historical_rows = envelope.get("historical_evidence") or []
        historical_ids = set()
        for historical in historical_rows:
            history_id = historical.get("id")
            if history_id:
                historical_ids.add(history_id)
            if (
                historical.get("temporal_status") != "historical"
                or historical.get("organisation_number") != org
                or historical.get("record") is None
            ):
                provenance_errors.append({
                    "organisation_number": org,
                    "reason": "historical evidence is missing its explicit historical identity",
                })

        claims = envelope.get("claims") or []

        for claim_index, claim in enumerate(claims):
            state = claim.get("availability")

            if state not in AVAILABILITY_STATES:
                invalid_states.append(
                    {
                        "organisation_number": org,
                        "claim_index": claim_index,
                        "state": state,
                    }
                )

            refs = claim.get("evidence_ids")

            if not isinstance(refs, list) or not refs:
                dangling_evidence_refs.append(
                    {
                        "organisation_number": org,
                        "claim_index": claim_index,
                        "reason": "missing evidence references",
                    }
                )
                continue

            for evidence_id in refs:
                if evidence_id not in evidence_ids:
                    if evidence_id in historical_ids:
                        historical_reference_errors.append({
                            "organisation_number": org,
                            "claim_index": claim_index,
                            "evidence_id": evidence_id,
                            "reason": "historical evidence cannot support a current claim",
                        })
                        continue
                    dangling_evidence_refs.append(
                        {
                            "organisation_number": org,
                            "claim_index": claim_index,
                            "evidence_id": evidence_id,
                            "reason": "evidence reference not found",
                        }
                    )
                    continue

                if state == "available":
                    item = evidence_by_id[evidence_id]
                    source_class = item.get("source_class")
                    expected_rights = SOURCE_RIGHTS_STATUS.get(source_class)
                    if not _valid_source_url(item.get("source_url")):
                        provenance_errors.append({
                            "organisation_number": org,
                            "claim_index": claim_index,
                            "evidence_id": evidence_id,
                            "reason": "available claim source URL is missing or invalid",
                        })
                    if not _timestamp_is_valid(item.get("retrieved_at")):
                        provenance_errors.append({
                            "organisation_number": org,
                            "claim_index": claim_index,
                            "evidence_id": evidence_id,
                            "reason": "available claim retrieval timestamp is missing or invalid",
                        })
                    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(item.get("content_sha256") or "")):
                        provenance_errors.append({
                            "organisation_number": org,
                            "claim_index": claim_index,
                            "evidence_id": evidence_id,
                            "reason": "available claim content hash is missing or invalid",
                        })
                    if not _identity_proof_is_valid(item, org):
                        provenance_errors.append({
                            "organisation_number": org,
                            "claim_index": claim_index,
                            "evidence_id": evidence_id,
                            "reason": "available claim lacks exact organisation identity proof",
                        })
                    if expected_rights is None or item.get("rights_status") != expected_rights:
                        provenance_errors.append({
                            "organisation_number": org,
                            "claim_index": claim_index,
                            "evidence_id": evidence_id,
                            "reason": "available claim has an unknown or inconsistent source rights status",
                        })
                    if (
                        str(claim.get("field") or "").startswith("financials.records[")
                        and not item.get("reporting_periods")
                    ):
                        provenance_errors.append({
                            "organisation_number": org,
                            "claim_index": claim_index,
                            "evidence_id": evidence_id,
                            "reason": "financial claim is missing its reporting period",
                        })

    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": (
            len(organisation_numbers) == len(set(organisation_numbers))
        ),
        "required_sections_present": not missing_sections,
        "legal_identity_matches_envelope": not identity_errors,
        "all_claim_states_allowed": not invalid_states,
        "all_claim_evidence_references_resolve": (
            not dangling_evidence_refs
        ),
        "available_claims_have_valid_provenance": not provenance_errors,
        "historical_evidence_is_not_used_for_current_claims": not historical_reference_errors,
        "refresh_metadata_is_valid": not refresh_errors,
    }

    return {
        "passed": all(checks.values()),
        "checks": checks,
        "invalid_states": invalid_states,
        "dangling_evidence_refs": dangling_evidence_refs,
        "missing_sections": missing_sections,
        "identity_errors": identity_errors,
        "provenance_errors": provenance_errors,
        "historical_reference_errors": historical_reference_errors,
        "refresh_errors": refresh_errors,
    }
