#!/usr/bin/env python3
"""Build source-linked Signalpost briefs with a strict relevance/context split."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.external_footprint import publishable_observation  # noqa: E402


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _valid_timestamp(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return "T" in raw
    except ValueError:
        return False


def _record_source(record: dict[str, Any], module: str, classification: str) -> dict[str, Any] | None:
    if record.get("status") != "available":
        return None
    url = str(record.get("source_url") or "").strip()
    digest = str(record.get("content_sha256") or "").casefold()
    retrieved = str(record.get("retrieved_at") or "").strip()
    if not url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.casefold()
        host = parsed.hostname or ""
        port = parsed.port
        if scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
            return None
        if port not in {None, 80 if scheme == "http" else 443}:
            return None
        if "[" in parsed.netloc or "]" in parsed.netloc or ":" in host:
            if ipaddress.ip_address(host).version != 6:
                return None
        else:
            ascii_host = host.encode("idna").decode("ascii")
            if ascii_host.endswith(".") or any(
                not label or len(label) > 63
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                for label in ascii_host.split(".")
            ):
                return None
    except (ValueError, UnicodeError):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or not _valid_timestamp(retrieved):
        return None
    return {
        "module": module,
        "classification": classification,
        "source_url": url,
        "retrieved_at": retrieved,
        "content_sha256": digest,
    }


def _external_source(item: dict[str, Any]) -> dict[str, Any] | None:
    return _record_source(
        {
            "status": "available",
            "source_url": item.get("source_url"),
            "retrieved_at": item.get("retrieved_at"),
            "content_sha256": item.get("content_sha256"),
        },
        str(item.get("platform") or "external"),
        str(item.get("source_class") or "verified_external_observation"),
    )


def _latest_financial(row: dict[str, Any]) -> dict[str, Any]:
    records = ((row.get("evidence", {}).get("financials", {}).get("value") or {}).get("records") or [])
    if not records:
        return {}
    dated = [( _parse_date((item.get("period") or {}).get("tilDato")), index, item)
             for index, item in enumerate(records)]
    valid = [entry for entry in dated if entry[0] is not None]
    if valid:
        return max(valid, key=lambda entry: (entry[0], -entry[1]))[2]
    return records[0]


def _pretty_day(value: date) -> str:
    return f"{value.day} {value.strftime('%b %Y')}"


def _format_period(period: Any) -> str:
    if isinstance(period, dict):
        start = _parse_date(period.get("fraDato"))
        end = _parse_date(period.get("tilDato"))
        if start and end:
            if start.year == end.year:
                return f"FY{end.year} ({_pretty_day(start)}–{_pretty_day(end)})"
            return f"{_pretty_day(start)}–{_pretty_day(end)}"
        year = period.get("aar") or period.get("year")
        if str(year or "").isdigit():
            return f"FY{year}"
    if isinstance(period, str) and period.strip():
        return period.strip()
    return "the latest returned reporting period"


def _format_amount(value: Any, currency: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return None
    except (InvalidOperation, ValueError):
        return None
    if amount == amount.to_integral_value():
        formatted = f"{amount:,.0f}"
    else:
        formatted = f"{amount:,.2f}"
    code = str(currency or "").strip().upper()
    return f"{code} {formatted}" if code else f"{formatted} (currency not reported)"


def _current_job(item: dict[str, Any], today: date) -> bool:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    status = str(item.get("status") or metrics.get("status") or "").strip().upper()
    if status and status != "ACTIVE":
        return False
    if item.get("active") is False or metrics.get("active") is False:
        return False
    expiry_value = item.get("expires_at") or item.get("expiry_date") or metrics.get("expires_at") or metrics.get("expiry_date")
    expiry = _parse_date(expiry_value)
    if expiry_value and expiry is None:
        return False
    if expiry and expiry <= today:
        return False
    # NAV's feed reports ACTIVE status; a future expiry is also usable where
    # the normalized observation did not retain the status field.
    return status == "ACTIVE" or expiry is not None or item.get("active") is True or metrics.get("active") is True


def _observation_published_date(item: dict[str, Any]) -> date | None:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    return _parse_date(
        item.get("published_at") or item.get("date_published")
        or metrics.get("published_at") or metrics.get("date_published")
    )


def build_brief(
    row: dict[str, Any],
    external_observations: list[dict[str, Any]] | None = None,
    *,
    as_of: date | str | None = None,
    max_age_days: int = 365,
) -> dict[str, Any]:
    """Build a deterministic brief; registry/financial facts are context, not traction."""
    if max_age_days < 1:
        raise ValueError("max_age_days must be positive")
    today = _parse_date(as_of) or date.today()
    cutoff = today - timedelta(days=max_age_days)
    evidence = row.get("evidence") or {}
    registry = evidence.get("registry") or {}
    financial = evidence.get("financials") or {}
    website = evidence.get("website") or {}
    website_value = website.get("value") or {}
    identity = website_value.get("identity_assessment") or {}
    external_observations = external_observations or []

    sources: list[dict[str, Any]] = []
    summary_parts: list[str] = []
    context: list[dict[str, Any]] = []
    indicators: list[dict[str, Any]] = []
    name = str(row.get("name") or "Unnamed registered entity")
    org = str(row.get("organisation_number") or "")

    registry_source = _record_source(registry, "registry", "official_registry_fact")
    if registry_source:
        sources.append(registry_source)
        descriptors: list[str] = []
        if row.get("legal_form"):
            descriptors.append(f"registered as {row['legal_form']}")
        if row.get("municipality"):
            descriptors.append(f"in {row['municipality']}")
        industry = row.get("industry_label") or row.get("industry_code")
        if industry:
            descriptors.append(f"industry classification: {industry}")
            context.append({
                "reason": f"Registered industry classification: {industry}.",
                "basis": "official_registry_fact",
                "sources": [registry_source["source_url"]],
            })
        employees = row.get("employees")
        if employees is not None:
            descriptors.append(f"registry-reported employees: {employees}")
            context.append({
                "reason": f"The registry snapshot reports {employees} employees; this is not a live headcount.",
                "basis": "official_registry_fact",
                "sources": [registry_source["source_url"]],
            })
        if descriptors:
            summary_parts.append(f"{name} is " + ", ".join(descriptors) + ".")

    latest = _latest_financial(row)
    financial_source = _record_source(financial, "financials", "official_annual_account")
    if latest and financial_source:
        financial_parts: list[str] = []
        revenue = _format_amount(latest.get("revenue"), latest.get("currency"))
        operating = _format_amount(latest.get("operating_result"), latest.get("currency"))
        annual = _format_amount(latest.get("annual_result"), latest.get("currency"))
        if revenue:
            financial_parts.append(f"revenue of {revenue}")
        if operating:
            financial_parts.append(f"an operating result of {operating}")
        elif annual:
            financial_parts.append(f"an annual result of {annual}")
        if financial_parts:
            period_label = _format_period(latest.get("period"))
            summary_parts.append(f"For {period_label}, its annual accounts report " + " and ".join(financial_parts) + ".")
            sources.append(financial_source)
            context.append({
                "reason": f"Financial scale/context is available for {period_label}: " + ", ".join(financial_parts) + ".",
                "basis": "official_annual_account",
                "sources": [financial_source["source_url"]],
                "reporting_period": period_label,
            })

    if website.get("status") == "available" and identity.get("publishable") is True and identity.get("status") == "exact":
        website_source = _record_source(website, "website", "company_reported_claim")
        description = str(website_value.get("description") or "").strip()
        if description and website_source:
            summary_parts.append(f"Its verified company website describes its activities as: {description[:350]}.")
            sources.append(website_source)
            context.append({
                "reason": "The exact-identity company website describes the entity's stated activities; this is company-reported, not independent validation.",
                "basis": "company_reported_claim",
                "sources": [website_source["source_url"]],
            })

    for item in external_observations:
        if not publishable_observation(
            item,
            expected_organisation_number=org,
            expected_company_name=name,
            expected_website_url=website_value.get("final_url") or row.get("website"),
            as_of=today,
        ):
            continue
        source = _external_source(item)
        if not source:
            continue
        published_date = _observation_published_date(item)
        if not published_date or published_date > today or published_date < cutoff:
            continue
        signal_type = item.get("signal_type")
        platform = item.get("platform")
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        if signal_type == "job_posting" and platform in {"job_board", "linkedin", "indeed"}:
            if not _current_job(item, today):
                continue
            title = str(metrics.get("title") or item.get("evidence_span") or "A vacancy").strip()
            expiry = _parse_date(item.get("expires_at") or metrics.get("expires_at"))
            reason = f"An active vacancy for {title[:180]} was published on {published_date.isoformat()}"
            if expiry:
                reason += f" and is listed through {expiry.isoformat()}"
            reason += ". This is a hiring signal, not proof of company growth."
            indicators.append({
                "reason": reason,
                "basis": "exact_entity_job_posting",
                "published_at": published_date.isoformat(),
                "evidence": {
                    "source_url": source["source_url"],
                    "retrieved_at": source["retrieved_at"],
                    "content_sha256": source["content_sha256"],
                },
            })
            summary_parts.append(f"A current vacancy for {title[:180]} was observed on {published_date.isoformat()}.")
            sources.append(source)
        elif signal_type == "public_post" and platform == "company_site":
            title = str(item.get("evidence_span") or "Dated company update").strip()
            indicators.append({
                "reason": f"The exact-identity company website published a dated update on {published_date.isoformat()}: {title[:220]}. This is company-owned activity, not independent news or sentiment.",
                "basis": "dated_company_owned_update",
                "published_at": published_date.isoformat(),
                "evidence": {
                    "source_url": source["source_url"],
                    "retrieved_at": source["retrieved_at"],
                    "content_sha256": source["content_sha256"],
                },
            })
            summary_parts.append(f"Its company website published an update on {published_date.isoformat()}.")
            sources.append(source)
        elif signal_type == "public_mention" and item.get("platform") == "news" and item.get("source_class") in {"licensed_news", "public_news", "public_mention"}:
            excerpt = str(item.get("evidence_span") or "").strip()
            if not excerpt:
                continue
            indicators.append({
                "reason": f"An independently sourced, exact-company public mention dated {published_date.isoformat()} was captured: {excerpt[:220]}.",
                "basis": "dated_independent_public_mention",
                "published_at": published_date.isoformat(),
                "evidence": {
                    "source_url": source["source_url"],
                    "retrieved_at": source["retrieved_at"],
                    "content_sha256": source["content_sha256"],
                },
            })
            sources.append(source)

    summary = " ".join(summary_parts).strip()
    if not summary:
        summary = "A concise company summary cannot be produced from the available source-linked records without adding unsupported assumptions."

    if any(item["basis"] == "dated_independent_public_mention" for item in indicators):
        relevance_status = "independent_external_signals_available"
        relevance_note = "At least one dated, exact-company signal from an independently sourced publisher is available; each item is contextual, not a conclusion about company quality."
    elif any(item["basis"] == "exact_entity_job_posting" for item in indicators):
        relevance_status = "verified_hiring_signals_available"
        relevance_note = "Exact-employer vacancies are observable hiring signals, not proof of growth or business quality."
    elif indicators:
        relevance_status = "company_owned_activity_only"
        relevance_note = "Only company-owned dated activity is available; it is not independent validation or sentiment."
    elif context:
        relevance_status = "context_only"
        relevance_note = "Registry and/or annual-account facts provide company context, but no qualifying external relevance signal was found in this snapshot."
    else:
        relevance_status = "insufficient_evidence"
        relevance_note = "No supported company context or external relevance indicator was available in this snapshot. Do not infer business quality, popularity, growth, or suitability."

    unique_sources = {(item["module"], item["source_url"]): item for item in sources}
    return {
        "organisation_number": org,
        "company_name": name,
        "company_summary": summary,
        "company_context": {
            "status": "available" if context else "not_available",
            "items": context,
        },
        "why_relevant": {
            "status": relevance_status,
            "note": relevance_note,
            "indicators": indicators,
        },
        "sources": sorted(unique_sources.values(), key=lambda item: (item["module"], item["source_url"])),
        "claim_policy": "Every factual summary statement and external relevance indicator must resolve to a direct source URL, retrieval timestamp, and SHA-256 evidence hash; missing data is not zero.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build source-linked company briefs; separate context from verified external relevance.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--external-observations", nargs="*", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--as-of", help="Optional YYYY-MM-DD date for reproducible freshness checks")
    parser.add_argument("--max-age-days", type=int, default=365)
    args = parser.parse_args()
    profiles = [json.loads(line) for line in Path(args.profiles).read_text(encoding="utf-8").splitlines() if line.strip()]
    external_by_org: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for filename in args.external_observations:
        for item in (json.loads(line) for line in Path(filename).read_text(encoding="utf-8").splitlines() if line.strip()):
            external_by_org[str(item.get("organisation_number") or "")].append(item)
    as_of = _parse_date(args.as_of) if args.as_of else None
    if args.as_of and as_of is None:
        raise SystemExit("--as-of must be a valid YYYY-MM-DD date")
    rows = [build_brief(row, external_by_org.get(str(row.get("organisation_number") or ""), []), as_of=as_of, max_age_days=args.max_age_days) for row in profiles]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temp.replace(output)
    statuses = {key: sum(row["why_relevant"]["status"] == key for row in rows) for key in (
        "independent_external_signals_available", "verified_hiring_signals_available", "company_owned_activity_only", "context_only", "insufficient_evidence"
    )}
    print(json.dumps({
        "profiles": len(profiles),
        "briefs_written": len(rows),
        "external_relevance_indicators": sum(bool(row["why_relevant"]["indicators"]) for row in rows),
        "status_counts": statuses,
        "company_context_available": sum(row["company_context"]["status"] == "available" for row in rows),
        "source_linked_briefs": sum(bool(row["sources"]) for row in rows),
        "claim_boundary": "Registry/financial facts are context, not external traction. Only dated, exact-entity, source-hashed external observations count as relevance signals.",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
