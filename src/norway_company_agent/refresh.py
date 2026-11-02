from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
    "registry.name": ("name",),
    "registry.legal_form": ("legal_form",),
    "registry.employees": ("employees",),
    "registry.municipality": ("municipality",),
    "registry.website": ("website",),
    "registry.latest_submitted_accounts": ("latest_submitted_accounts",),
    "financials.records": ("evidence", "financials", "value", "records"),
    "financial_history.years": ("evidence", "financial_history", "value", "years"),
    "roles.roles": ("evidence", "roles", "value", "roles"),
    "locations.locations": ("evidence", "locations", "value", "locations"),
    "website.title": ("evidence", "website", "value", "title"),
    "website.description": ("evidence", "website", "value", "description"),
    "website.social_links": ("evidence", "website", "value", "social_links"),
}


def _read(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _evidence_for(profile: dict[str, Any], field: str) -> dict[str, Any]:
    module = field.split(".", 1)[0]
    records = profile.get("evidence", {})
    if module == "registry":
        return records.get("registry_live") or records.get("registry", {})
    return records.get(module, {})


def _change_value(field: str, value: Any) -> Any:
    # Website text and social links may come from a registry-linked but unverified
    # parent/brand site. The refresh ledger records the change without retaining it.
    if field.startswith("website."):
        return {"redacted": True, "present": value not in (None, "", [], {})}
    return value


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _evidence_signature(record: dict[str, Any] | None) -> str:
    if not isinstance(record, dict):
        return ""
    stable = {
        key: record.get(key)
        for key in (
            "status",
            "source_class",
            "source_type",
            "source_url",
            "content_sha256",
            "source_row_key",
            "effective_at",
            "as_of",
            "rights_status",
            "value",
        )
        if key in record
    }
    return hashlib.sha256(_stable_json(stable).encode("utf-8")).hexdigest()


def _has_source_evidence(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("source_class") != "agent_execution"
        and record.get("source_type") != "agent_execution"
        and any(
            record.get(key) not in (None, "", [], {})
            for key in ("source_url", "content_sha256", "source_row_key", "value")
        )
    )


def _historical_key(module: str, record: dict[str, Any]) -> tuple[str, str]:
    return module, _evidence_signature(record)


def diff_profile(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    observed_modules: set[str] | None = None,
) -> list[dict[str, Any]]:
    old_org = previous.get("organisation_number")
    new_org = current.get("organisation_number")
    if not old_org or old_org != new_org:
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = []
    for field, path in TRACKED_FIELDS.items():
        module = field.split(".", 1)[0]
        if observed_modules is not None and module not in observed_modules:
            continue
        old_value = _read(previous, path)
        new_value = _read(current, path)
        if old_value == new_value:
            continue
        record = _evidence_for(current, field)
        previous_record = _evidence_for(previous, field)
        changes.append({
            "organisation_number": new_org,
            "field": field,
            "old_value": _change_value(field, old_value),
            "new_value": _change_value(field, new_value),
            "source_url": record.get("source_url"),
            "retrieved_at": record.get("retrieved_at"),
            "effective_at": record.get("effective_at") or record.get("as_of"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "old_content_sha256": previous_record.get("content_sha256"),
            "new_content_sha256": record.get("content_sha256"),
            "status": record.get("status"),
        })

    previous_evidence = previous.get("evidence") or {}
    current_evidence = current.get("evidence") or {}
    evidence_modules = set(previous_evidence) | set(current_evidence)
    if observed_modules is not None:
        evidence_modules &= observed_modules

    for module in sorted(evidence_modules):
        old_record = previous_evidence.get(module)
        new_record = current_evidence.get(module)
        old_present = _has_source_evidence(old_record)
        new_present = _has_source_evidence(new_record)
        if not old_present and not new_present:
            continue
        if not old_present:
            status = "added"
        elif not new_present:
            status = "removed"
        elif _evidence_signature(old_record) != _evidence_signature(new_record):
            status = "changed"
        else:
            continue

        changes.append({
            "organisation_number": new_org,
            "field": f"evidence.{module}",
            "status": status,
            "old_value": (
                {"redacted": True, "present": old_present}
                if module == "website"
                else {
                    "status": (old_record or {}).get("status"),
                    "content_sha256": (old_record or {}).get("content_sha256"),
                }
            ),
            "new_value": (
                {"redacted": True, "present": new_present}
                if module == "website"
                else {
                    "status": (new_record or {}).get("status"),
                    "content_sha256": (new_record or {}).get("content_sha256"),
                }
            ),
            "old_content_sha256": (old_record or {}).get("content_sha256"),
            "new_content_sha256": (new_record or {}).get("content_sha256"),
            "source_url": (new_record or old_record or {}).get("source_url"),
            "retrieved_at": (new_record or {}).get("retrieved_at"),
            "effective_at": (new_record or {}).get("effective_at") or (new_record or {}).get("as_of"),
            "source_class": (new_record or old_record or {}).get("source_class") or (new_record or old_record or {}).get("source_type"),
        })
    return changes


def diff_datasets(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_orgs = [str(row.get("organisation_number") or "") for row in previous]
    new_orgs = [str(row.get("organisation_number") or "") for row in current]
    if (
        any(not org for org in old_orgs + new_orgs)
        or len(old_orgs) != len(set(old_orgs))
        or len(new_orgs) != len(set(new_orgs))
    ):
        raise ValueError("Refresh datasets require unique exact organisation numbers")
    old_by_org = {row["organisation_number"]: row for row in previous}
    new_by_org = {row["organisation_number"]: row for row in current}
    if set(old_by_org) != set(new_by_org):
        raise ValueError("Refresh datasets must have identical organisation-number membership")
    return [
        change
        for org in sorted(old_by_org)
        for change in diff_profile(old_by_org[org], new_by_org[org])
    ]


def preserve_previous_evidence(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Return a current profile with prior source records archived as history.

    Historical records stay outside ``evidence`` so they cannot support current
    claims. Records with identical content are deduplicated by their stable
    evidence signature, making repeated refreshes idempotent.
    """
    merged = copy.deepcopy(current)
    history: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def retain(module: str, record: Any) -> None:
        if not isinstance(record, dict) or not _has_source_evidence(record):
            return
        key = _historical_key(module, record)
        if key in seen:
            return
        seen.add(key)
        history.append({
            "module": module,
            "temporal_status": "historical",
            "record": copy.deepcopy(record),
        })

    for item in previous.get("historical_evidence") or []:
        if isinstance(item, dict):
            retain(str(item.get("module") or "unknown"), item.get("record"))

    old_evidence = previous.get("evidence") or {}
    new_evidence = current.get("evidence") or {}
    for module, old_record in sorted(old_evidence.items()):
        new_record = new_evidence.get(module)
        old_signature = _evidence_signature(old_record)
        new_signature = _evidence_signature(new_record)
        old_retrieved_at = (old_record or {}).get("retrieved_at") if isinstance(old_record, dict) else None
        new_retrieved_at = (new_record or {}).get("retrieved_at") if isinstance(new_record, dict) else None
        if old_signature and (
            old_signature != new_signature
            or old_retrieved_at != new_retrieved_at
        ):
            retain(module, old_record)

    merged["historical_evidence"] = history
    return merged


def profile_snapshot_sha256(profile: dict[str, Any]) -> str:
    """Hash evidence content without retrieval-time noise."""
    evidence_rows = profile.get("evidence") or {}
    stable = {
        "organisation_number": profile.get("organisation_number"),
        "evidence": {
            module: _evidence_signature(record)
            for module, record in sorted(evidence_rows.items())
        },
    }
    return hashlib.sha256(_stable_json(stable).encode("utf-8")).hexdigest()


def refresh_profile(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    observed_modules: set[str] | None = None,
) -> dict[str, Any]:
    """Attach deterministic refresh metadata and prior evidence to a profile."""
    if previous.get("organisation_number") != current.get("organisation_number"):
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = diff_profile(previous, current, observed_modules=observed_modules)
    refreshed = preserve_previous_evidence(previous, current)
    previous_hash = profile_snapshot_sha256(previous)
    current_hash = profile_snapshot_sha256(current)
    refreshed["refresh_checked"] = True
    refreshed["previous_snapshot_available"] = True
    refreshed["changes"] = changes
    refreshed["refresh_metadata"] = {
        "status": "checked",
        "previous_snapshot_available": True,
        "previous_snapshot_sha256": previous_hash,
        "current_snapshot_sha256": current_hash,
        "idempotent": not changes and previous_hash == current_hash,
        "change_count": len(changes),
    }
    return refreshed
