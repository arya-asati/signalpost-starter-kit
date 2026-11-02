#!/usr/bin/env python3
"""Extract dated activity from a verified, company-controlled website snapshot.

An archive/category page is not treated as a news item. Publication requires
page-level publication-date metadata captured by website.py, a deep article
path, exact legal-entity website identity, and a content hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

NEWS_PATH = re.compile(r"/(?:news|newsroom|press|press-releases|presse|media|aktuelt|nyheter|artikler|blog|updates|oppdateringer|innsikt|insights|publikasjoner)(?:/|$)", re.I)


def parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def observations(
    profile: dict,
    *,
    now: datetime | None = None,
    max_age_days: int = 365,
    limit: int = 5,
) -> list[dict]:
    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}
    if website.get("status") != "available" or not identity.get("publishable"):
        return []

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now_utc - timedelta(days=max_age_days)
    org = str(profile.get("organisation_number") or "")
    if not org.isdigit() or len(org) != 9:
        return []

    candidates: list[tuple[datetime, dict]] = []
    for page in value.get("pages") or []:
        url = str(page.get("url") or "")
        parsed_url = urlparse(url)
        parts = [part for part in parsed_url.path.split("/") if part]
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            continue
        if not NEWS_PATH.search(parsed_url.path) or len(parts) < 2:
            continue
        published = parse_datetime(page.get("published_at"))
        if published is None or published < cutoff or published > now_utc + timedelta(days=1):
            continue
        digest = str(page.get("content_sha256") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        title = str(page.get("title") or "").strip()
        excerpt = str(page.get("main_text_excerpt") or "").strip()
        if not title or not excerpt:
            continue
        candidates.append((published, page))

    candidates.sort(key=lambda pair: (pair[0], str(pair[1].get("url") or "")), reverse=True)
    rows: list[dict] = []
    seen_urls: set[str] = set()
    for published, page in candidates:
        url = str(page["url"])
        if url in seen_urls:
            continue
        seen_urls.add(url)
        digest = str(page["content_sha256"]).casefold()
        published_text = published.isoformat().replace("+00:00", "Z")
        title = str(page.get("title") or "").strip()
        excerpt = re.sub(r"\s+", " ", str(page.get("main_text_excerpt") or "")).strip()
        rows.append({
            "id": "company-site-news-" + hashlib.sha256(f"{org}|{url}|{published_text}".encode()).hexdigest()[:24],
            "organisation_number": org,
            "platform": "company_site",
            "signal_type": "public_post",
            "source_url": url,
            "retrieved_at": website.get("retrieved_at"),
            "published_at": published_text,
            "content_sha256": digest,
            "exact_entity": True,
            "identity_proof": [{
                "type": "website_identity_gate",
                "score": identity.get("score"),
                "method": identity.get("method"),
                "status": identity.get("status"),
            }],
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "company_site",
            "evidence_span": f"{title} — {excerpt[:900]}"[:1200],
            "metrics": {
                "published_at": published_text,
                "publication_date_source": page.get("publication_date_source"),
                "interpretation": "Dated company-owned activity; not independent news, customer opinion, or sentiment.",
            },
            "strategy": "dated_company_site_news_v2",
        })
        if len(rows) >= limit:
            break
    return rows


def observation(profile: dict) -> dict | None:
    """Compatibility helper: return the newest qualifying article, if any."""
    rows = observations(profile, limit=1)
    return rows[0] if rows else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract dated activity from exact-identity company-site article pages.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--max-age-days", type=int, default=365)
    parser.add_argument("--limit-per-company", type=int, default=5)
    args = parser.parse_args()
    if args.max_age_days < 1 or args.limit_per_company < 1:
        raise SystemExit("--max-age-days and --limit-per-company must be positive")

    profiles = [json.loads(line) for line in Path(args.profiles).read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [
        item
        for profile in profiles
        for item in observations(profile, max_age_days=args.max_age_days, limit=args.limit_per_company)
    ]
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    companies = {row["organisation_number"] for row in rows}
    report = {
        "connector": "dated_exact_company_site_news_v2",
        "profiles": len(profiles),
        "companies_with_dated_activity": len(companies),
        "observations": len(rows),
        "max_age_days": args.max_age_days,
        "claim_boundary": "Only dated, hashed article pages from exact-identity company websites are emitted. Company-owned activity is not independent news or sentiment.",
        "abstention_rule": "Undated pages, archive pages, weak identity, missing excerpts, and missing hashes are not published.",
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
