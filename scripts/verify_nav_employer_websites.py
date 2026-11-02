#!/usr/bin/env python3
"""Independently fetch NAV employer homepage candidates and apply Signalpost identity gates.

A NAV homepage URL is a lead, not proof. This verifier refuses experimental
NAV candidates and publishes a homepage only when the existing website identity
gate accepts the fetched page for the exact legal entity.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.run_nav_jobs_connector import registered_employer_entities  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temp.replace(path)


def verify_candidates(
    profiles: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    fetch_fn: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = fetch_website,
    max_candidates: int = 100,
    max_per_company: int = 2,
    timeout: float = 15.0,
    min_interval: float = 0.5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if max_candidates < 1 or max_per_company < 1:
        raise ValueError("candidate limits must be positive")
    by_org = {str(row.get("organisation_number") or ""): copy.deepcopy(row) for row in profiles}
    output = {org: copy.deepcopy(row) for org, row in by_org.items()}
    eligible: list[dict[str, Any]] = []
    skipped_experimental = 0
    skipped_already_verified = 0
    for candidate in candidates:
        org = str(candidate.get("organisation_number") or "")
        profile = output.get(org)
        if profile is None or candidate.get("candidate_status") != "discovered_not_verified":
            continue
        if candidate.get("rights_status") != "approved" or candidate.get("acquisition_mode") != "official_api":
            skipped_experimental += 1
            continue
        parent_org = str(candidate.get("parent_organisation_number") or org)
        employer_org = str(candidate.get("exact_employer_organisation_number") or "")
        known_employers = set(registered_employer_entities(profile))
        if parent_org != org or employer_org not in known_employers:
            continue
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(candidate.get("content_sha256") or "")):
            continue
        if not candidate.get("identity_proof") or not candidate.get("retrieved_at"):
            continue
        current = (profile.get("evidence") or {}).get("website") or {}
        current_identity = ((current.get("value") or {}).get("identity_assessment") or {})
        if current.get("status") == "available" and current_identity.get("publishable"):
            skipped_already_verified += 1
            continue
        url = str(candidate.get("candidate_url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            continue
        eligible.append(candidate)
    eligible.sort(key=lambda item: (str(item.get("organisation_number") or ""), str(item.get("candidate_url") or "")))
    eligible = eligible[:max_candidates]

    per_company: Counter[str] = Counter()
    attempts = 0
    verified = 0
    quarantined = 0
    fetch_errors = 0
    requests = 0
    bytes_received = 0
    latencies: list[int] = []
    status_counts: Counter[str] = Counter()
    for candidate in eligible:
        org = str(candidate["organisation_number"])
        if per_company[org] >= max_per_company:
            continue
        per_company[org] += 1
        attempts += 1
        profile = output[org]
        try:
            website_record, metrics = fetch_fn(str(candidate["candidate_url"]), timeout=timeout)
        except Exception as exc:  # preserve one company when an individual fetch crashes
            fetch_errors += 1
            profile.setdefault("website_discovery_attempts", []).append({
                "candidate_url": candidate["candidate_url"],
                "status": "worker_error",
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                "source_url": candidate.get("source_url"),
            })
            continue

        requests += int(metrics.get("requests") or 0)
        bytes_received += int(metrics.get("bytes") or 0)
        latencies.extend(int(value) for value in (metrics.get("latencies_ms") or []) if value is not None)
        status_counts[str(website_record.get("status") or "unknown")] += 1
        attempt = {
            "candidate_url": candidate["candidate_url"],
            "source_url": candidate.get("source_url"),
            "candidate_source_retrieved_at": candidate.get("retrieved_at"),
            "candidate_source_hash": candidate.get("content_sha256"),
            "fetch_status": website_record.get("status"),
            "fetch_metrics": metrics,
        }
        if website_record.get("status") != "available":
            profile.setdefault("website_discovery_attempts", []).append(attempt)
            continue

        gated = apply_website_identity_gate(profile, website_record)
        assessment = gated.get("assessment") or {}
        attempt["identity_assessment"] = assessment
        attempt["quarantined_social_links"] = gated.get("quarantined_social_links", 0)
        if assessment.get("publishable"):
            evidence = profile.setdefault("evidence", {})
            previous = evidence.get("website")
            if previous and previous.get("status") != "not_found":
                evidence["website_registry_original"] = previous
            value = (gated["website"].get("value") or {})
            value["discovery_provenance"] = {
                "method": "nav_employer_homepage_then_exact_website_identity_gate_v1",
                "source_url": candidate.get("source_url"),
                "source_retrieved_at": candidate.get("retrieved_at"),
                "source_content_sha256": candidate.get("content_sha256"),
                "employer_organisation_number": candidate.get("exact_employer_organisation_number"),
            }
            gated["website"]["value"] = value
            gated["website"]["source_type"] = "nav_discovered_company_website"
            gated["website"]["source_class"] = "independently_verified_company_website"
            evidence["website"] = gated["website"]
            profile["website"] = value.get("final_url") or candidate["candidate_url"]
            verified += 1
            attempt["status"] = "verified_and_promoted"
        else:
            quarantined += 1
            attempt["status"] = "quarantined_identity_not_proven"
            attempt["identity_reasons"] = assessment.get("reasons") or []
            # Keep fetched content as an audit artifact, but do not replace the
            # canonical website record or set the profile's website field.
            attempt["website_evidence"] = gated["website"]
        profile.setdefault("website_discovery_attempts", []).append(attempt)
        if min_interval:
            time.sleep(min_interval)

    report = {
        "connector": "nav_employer_homepage_independent_verification_v1",
        "profiles_in": len(profiles),
        "candidate_records_in": len(candidates),
        "eligible_candidates": len(eligible),
        "attempts": attempts,
        "verified_and_promoted": verified,
        "quarantined_identity_not_proven": quarantined,
        "fetch_errors": fetch_errors,
        "skipped_experimental_or_unapproved": skipped_experimental,
        "skipped_already_verified": skipped_already_verified,
        "website_status_counts": dict(sorted(status_counts.items())),
        "requests": requests,
        "bytes_received": bytes_received,
        "latency_samples": len(latencies),
        "latency_p50_ms": sorted(latencies)[len(latencies) // 2] if latencies else None,
        "claim_boundary": "NAV homepage is only a candidate. A URL enters the canonical website field only after an independent fetch and Signalpost exact-legal-entity identity gate pass.",
    }
    return [output[str(row.get("organisation_number") or "")] for row in profiles], report


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify NAV employer homepage candidates with Signalpost's existing identity gate.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--output", required=True, help="New profiles JSONL; source profiles are never overwritten")
    parser.add_argument("--report", required=True)
    parser.add_argument("--max-candidates", type=int, default=100)
    parser.add_argument("--max-per-company", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--min-interval", type=float, default=0.5)
    args = parser.parse_args()
    profiles = read_jsonl(Path(args.profiles))
    candidates = read_jsonl(Path(args.candidates))
    updated, report = verify_candidates(
        profiles,
        candidates,
        max_candidates=args.max_candidates,
        max_per_company=args.max_per_company,
        timeout=args.timeout,
        min_interval=args.min_interval,
    )
    write_jsonl(Path(args.output), updated)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
