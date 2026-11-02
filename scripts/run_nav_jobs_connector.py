#!/usr/bin/env python3
"""Discover current Norwegian vacancies from NAV and verify the exact employer.

The feed is free but authenticated. A private token is preferred for production.
The rotating public token may be used only for experiments; those observations
are deliberately marked non-publishable until the consumer's production access
and terms review are complete.

No applicant/contact personal data is written to output.
"""
from __future__ import annotations

import argparse
import difflib
import email.utils
import hashlib
import http.client
import json
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FEED_BASE = "https://pam-stilling-feed.nav.no"
FEED_URL = FEED_BASE + "/api/v1/feed"
PUBLIC_TOKEN_URL = FEED_BASE + "/api/publicToken"
USER_AGENT = "signalpost-company-research/1.0 (NAV public job-feed consumer)"
LEGAL_SUFFIXES = {"as", "asa", "nuf", "ans", "da", "sa", "sti", "brl", "ik"}
BRREG_SUBUNITS_HOST = "data.brreg.no"
BRREG_SUBUNITS_PATH = "/enhetsregisteret/api/underenheter"
NAV_HOST = "pam-stilling-feed.nav.no"
NEAR_MATCH_RETAIN_LIMIT = 50
TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    socket.timeout,
    ConnectionError,
    OSError,
)
RECOVERABLE_REQUEST_ERRORS = TRANSPORT_ERRORS + (ValueError,)


def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalise_company_name(value: Any) -> str:
    raw = str(value or "").casefold().translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"}))
    text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def employer_name_similarity(profile_name: str, feed_name: str) -> dict[str, float]:
    """Symmetric, review-only name similarity; never authorises a published claim."""
    left = normalise_company_name(profile_name)
    right = normalise_company_name(feed_name)
    if not left or not right:
        return {"score": 0.0, "token_dice": 0.0, "sequence_ratio": 0.0}
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    shared = len(left_tokens & right_tokens)
    token_dice = (2.0 * shared / (len(left_tokens) + len(right_tokens))) if (left_tokens or right_tokens) else 0.0
    sequence_ratio = difflib.SequenceMatcher(None, left, right).ratio()
    return {
        "score": max(token_dice, sequence_ratio),
        "token_dice": token_dice,
        "sequence_ratio": sequence_ratio,
    }


def is_name_candidate(profile_name: str, feed_name: str) -> bool:
    """Loose pre-filter only; it never authorises publication.

    A name match is used only to decide which detail pages are worth fetching.
    `build_job_observation` still requires the exact nine-digit employer number.
    """
    left = normalise_company_name(profile_name)
    right = normalise_company_name(feed_name)
    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens, right_tokens = set(left.split()), set(right.split())
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens))
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    return overlap >= 0.90 or ratio >= 0.90


def parse_datetime(value: Any) -> datetime | None:
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


def _detail_body(payload: dict[str, Any]) -> dict[str, Any]:
    body = payload.get("json") or payload.get("ad_content") or payload.get("adContent") or payload
    return body if isinstance(body, dict) else {}


def _is_exact_brreg_subunits_source(source_url: Any, parent_org: str) -> bool:
    """Accept only the Brønnøysund subunit listing for this exact parent."""
    try:
        parsed = urllib.parse.urlparse(str(source_url or ""))
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == BRREG_SUBUNITS_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == BRREG_SUBUNITS_PATH
        and query.get("overordnetEnhet") == [parent_org]
    )



def registered_employer_entities(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map the parent legal entity and its Brønnøysund-verified subunits.

    NAV's employer.orgnr is a Virksomhetsnummer (underenhet), not necessarily
    the parent legal-entity organisation number. Subunits are trusted only
    when they come from a hashed, timestamped Brønnøysund locations record.
    """
    parent_org = digits(profile.get("organisation_number"))
    if len(parent_org) != 9:
        return {}
    entities: dict[str, dict[str, Any]] = {
        parent_org: {"name": str(profile.get("name") or ""), "relationship": "parent_entity"}
    }
    evidence = profile.get("evidence") or {}
    locations_record = evidence.get("locations") or {}
    locations_value = locations_record.get("value") or {}
    digest = str(locations_record.get("content_sha256") or "").casefold()
    retrieved_at = str(locations_record.get("retrieved_at") or "").strip()
    if (
        locations_record.get("status") != "available"
        or not _is_exact_brreg_subunits_source(locations_record.get("source_url"), parent_org)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not retrieved_at
    ):
        return entities
    for location in locations_value.get("locations") or []:
        subunit_org = digits(location.get("organisation_number"))
        if len(subunit_org) != 9:
            continue
        entities[subunit_org] = {
            "name": str(location.get("name") or ""),
            "relationship": "registered_subunit",
            "parent_organisation_number": parent_org,
            "source_url": locations_record.get("source_url"),
            "retrieved_at": retrieved_at,
            "content_sha256": digest,
        }
    return entities


def profile_employer_names(profile: dict[str, Any]) -> list[str]:
    """Names used only for pre-filtering; never sufficient to publish a job."""
    names = [str(profile.get("name") or "").strip()]
    locations = ((profile.get("evidence") or {}).get("locations") or {}).get("value") or {}
    names.extend(
        str(item.get("name") or "").strip()
        for item in locations.get("locations") or []
    )
    return list(dict.fromkeys(name for name in names if name))


def profile_matches_feed_names(profile: dict[str, Any], feed_names: list[str]) -> bool:
    return any(
        is_name_candidate(profile_name, feed_name)
        for profile_name in profile_employer_names(profile)
        for feed_name in feed_names
    )


def build_job_observation(
    profile: dict[str, Any],
    feed_item: dict[str, Any],
    detail_payload: dict[str, Any],
    raw_detail: bytes,
    *,
    retrieved_at: str,
    experimental_token: bool = False,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Build a strict observation; abstain unless legal-entity/date/status pass."""
    expected_org = digits(profile.get("organisation_number"))
    if len(expected_org) != 9:
        return None

    body = _detail_body(detail_payload)
    employer = body.get("employer") if isinstance(body.get("employer"), dict) else {}
    employer_org = digits(employer.get("orgnr") or employer.get("organisation_number") or employer.get("organisationNumber"))
    registered_entities = registered_employer_entities(profile)
    employer_entity = registered_entities.get(employer_org)
    if len(employer_org) != 9 or employer_entity is None:
        return None

    feed_meta = feed_item.get("_feed_entry") if isinstance(feed_item.get("_feed_entry"), dict) else {}
    statuses = [
        str(feed_meta.get("status") or "").upper(),
        str(detail_payload.get("status") or "").upper(),
        str(body.get("status") or "").upper(),
    ]
    known_statuses = [status for status in statuses if status]
    if not known_statuses or any(status != "ACTIVE" for status in known_statuses):
        return None

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    published = parse_datetime(body.get("published"))
    if published is None or published > now_utc + timedelta(days=1):
        return None
    expires = parse_datetime(body.get("expires"))
    if expires is not None and expires <= now_utc:
        return None

    title = str(body.get("title") or body.get("jobtitle") or feed_item.get("title") or "").strip()
    employer_name = str(employer.get("name") or feed_meta.get("businessName") or "").strip()
    if not title or not employer_name:
        return None

    ad_id = str(body.get("uuid") or detail_payload.get("uuid") or feed_meta.get("uuid") or feed_meta.get("id") or feed_item.get("id") or "").strip()
    if not ad_id:
        return None

    public_link = str(body.get("link") or body.get("sourceurl") or feed_item.get("link") or "").strip()
    detail_url = str(feed_item.get("url") or "").strip()
    source_url = public_link or detail_url
    if not source_url:
        source_url = f"{FEED_BASE}/api/v1/feedentry/{urllib.parse.quote(ad_id, safe='') }"
    parsed_url = urllib.parse.urlparse(source_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        return None

    location_parts: list[str] = []
    locations = body.get("workLocations") or []
    if isinstance(locations, list):
        for location in locations[:3]:
            if not isinstance(location, dict):
                continue
            city = str(location.get("city") or location.get("municipal") or "").strip()
            county = str(location.get("county") or "").strip()
            label = ", ".join(part for part in (city, county) if part)
            if label and label not in location_parts:
                location_parts.append(label)

    published_text = published.isoformat().replace("+00:00", "Z")
    expires_text = expires.isoformat().replace("+00:00", "Z") if expires else None
    employer_homepage = str(employer.get("homepage") or "").strip()
    if employer_homepage and not re.match(r"^https?://", employer_homepage, re.I):
        employer_homepage = "https://" + employer_homepage
    homepage_parsed = urllib.parse.urlparse(employer_homepage)
    if homepage_parsed.scheme not in {"http", "https"} or not homepage_parsed.hostname:
        employer_homepage = ""
    raw_hash = hashlib.sha256(raw_detail).hexdigest()
    acquisition_mode = "rights_review_experiment" if experimental_token else "official_api"
    rights_status = "review_required" if experimental_token else "approved"
    evidence_parts = [title, employer_name]
    if location_parts:
        evidence_parts.append(" / ".join(location_parts))
    evidence_parts.append(published_text[:10])

    return {
        "id": "nav-job-" + hashlib.sha256(ad_id.encode("utf-8")).hexdigest()[:24],
        "organisation_number": expected_org,
        "platform": "job_board",
        "signal_type": "job_posting",
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "published_at": published_text,
        "content_sha256": raw_hash,
        "exact_entity": True,
        "identity_proof": [
            {
                "type": "nav_employer_organisation_number",
                "expected_parent_organisation_number": expected_org,
                "observed_employer_organisation_number": employer_org,
                "match": "exact" if employer_org == expected_org else "exact_registered_subunit",
            },
            *(
                [{
                    "type": "brreg_registered_subunit_relationship",
                    "parent_organisation_number": expected_org,
                    "subunit_organisation_number": employer_org,
                    "subunit_name": employer_entity.get("name"),
                    "source_url": employer_entity.get("source_url"),
                    "retrieved_at": employer_entity.get("retrieved_at"),
                    "content_sha256": employer_entity.get("content_sha256"),
                    "match": "exact_parent_child",
                }]
                if employer_entity.get("relationship") == "registered_subunit"
                else []
            ),
        ],
        "acquisition_mode": acquisition_mode,
        "rights_status": rights_status,
        "source_class": "official_nav_job_feed",
        "evidence_span": " — ".join(evidence_parts)[:1200],
        "metrics": {
            "title": title,
            "employer_name": employer_name,
            "employer_organisation_number": employer_org,
            "employer_entity_relationship": employer_entity.get("relationship"),
            "employer_homepage": employer_homepage or None,
            "published_at": published_text,
            "expires_at": expires_text,
            "status": "ACTIVE",
            "work_locations": location_parts,
            "engagement_type": body.get("engagementtype"),
            "extent": body.get("extent"),
            "position_count": body.get("positioncount"),
            "sector": body.get("sector"),
            "application_due": body.get("applicationDue"),
        },
        "strategy": "nav_official_job_feed_exact_orgnr_v1",
    }


def build_website_candidate(
    profile: dict[str, Any],
    job_observation: dict[str, Any],
    detail_payload: dict[str, Any],
    raw_detail: bytes,
    *,
    retrieved_at: str,
    experimental_token: bool = False,
) -> dict[str, Any] | None:
    """Return a quarantinable homepage candidate; it is not an identity proof."""
    body = _detail_body(detail_payload)
    employer = body.get("employer") if isinstance(body.get("employer"), dict) else {}
    expected_org = digits(profile.get("organisation_number"))
    observed_org = digits(employer.get("orgnr") or employer.get("organisation_number") or employer.get("organisationNumber"))
    registered_entities = registered_employer_entities(profile)
    employer_entity = registered_entities.get(observed_org)
    if len(expected_org) != 9 or employer_entity is None:
        return None
    homepage = str(employer.get("homepage") or "").strip()
    if not homepage:
        return None
    if not re.match(r"^https?://", homepage, re.I):
        homepage = "https://" + homepage
    parsed = urllib.parse.urlparse(homepage)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    raw_hash = hashlib.sha256(raw_detail).hexdigest()
    acquisition_mode = "rights_review_experiment" if experimental_token else "official_api"
    rights_status = "review_required" if experimental_token else "approved"
    return {
        "id": "nav-employer-homepage-" + hashlib.sha256(f"{expected_org}|{homepage}".encode()).hexdigest()[:24],
        "organisation_number": expected_org,
        "candidate_url": homepage,
        "candidate_status": "discovered_not_verified",
        "source_url": job_observation.get("source_url"),
        "retrieved_at": retrieved_at,
        "content_sha256": raw_hash,
        "exact_employer_organisation_number": observed_org,
        "parent_organisation_number": expected_org,
        "identity_proof": list(job_observation.get("identity_proof") or []),
        "acquisition_mode": acquisition_mode,
        "rights_status": rights_status,
        "claim_boundary": "NAV supplied this homepage for an exact employer record; independent website fetch and Signalpost legal-entity identity gate are still required.",
    }


def _is_approved_nav_url(url: str) -> bool:
    """Accept only NAV's HTTPS endpoint on the standard HTTPS port."""
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == NAV_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.netloc.casefold() in {NAV_HOST, f"{NAV_HOST}:443"}
    )


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, request_counter: list[int]):
        super().__init__()
        self.request_counter = request_counter

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_approved_nav_url(req.full_url) or not _is_approved_nav_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "Unapproved NAV redirect destination blocked", headers, fp)
        # A redirect is another HTTP request, so it must be included in metrics.
        self.request_counter[0] += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str, headers: dict[str, str], timeout: float, counter: list[int]) -> bytes:
    if not _is_approved_nav_url(url):
        raise ValueError(f"Blocked unexpected NAV API URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers})
    opener = urllib.request.build_opener(_SameHostRedirectHandler(counter))
    counter[0] += 1
    with opener.open(request, timeout=timeout) as response:
        if not _is_approved_nav_url(response.geturl()):
            raise ValueError("NAV API redirected outside its approved HTTPS host and port")
        raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("NAV API response exceeded 5 MB safety limit")
        return raw


def _load_token(*, env_name: str, experimental_public_token: bool, timeout: float, counter: list[int]) -> tuple[str, bool]:
    import os

    private_token = os.environ.get(env_name, "").strip()
    if private_token:
        return private_token, False
    if not experimental_public_token:
        raise RuntimeError(
            f"No NAV token found. Set {env_name} for a private consumer token, "
            "or use --experimental-public-token for a small experiment after reviewing NAV's terms."
        )
    raw = _http_get(PUBLIC_TOKEN_URL, {}, timeout, counter)
    text = raw.decode("utf-8", errors="replace").strip()
    candidates = [line.strip() for line in text.splitlines() if line.strip()]
    token = candidates[-1] if candidates else ""
    if token.lower().startswith("current public token") or len(token.split(".")) != 3:
        raise RuntimeError("NAV public-token endpoint did not return a recognised JWT")
    return token, True


def _candidate_names(feed_item: dict[str, Any]) -> list[str]:
    """Return employer/business names only; vacancy titles are not employer evidence."""
    meta = feed_item.get("_feed_entry") if isinstance(feed_item.get("_feed_entry"), dict) else {}
    values = [meta.get("businessName"), feed_item.get("employer_name")]
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


def _feed_id(item: dict[str, Any]) -> str:
    meta = item.get("_feed_entry") if isinstance(item.get("_feed_entry"), dict) else {}
    return str(meta.get("uuid") or item.get("id") or "").strip()


def _event_datetime(item: dict[str, Any]) -> datetime:
    meta = item.get("_feed_entry") if isinstance(item.get("_feed_entry"), dict) else {}
    return parse_datetime(item.get("date_modified") or meta.get("sistEndret")) or datetime.min.replace(tzinfo=timezone.utc)


class _BoundedCandidateRows:
    """Retain only the highest ranked near-match rows."""

    def __init__(self, limit: int):
        self.limit = limit
        self.rows: list[dict[str, Any]] = []

    @staticmethod
    def _rank(row: dict[str, Any]) -> tuple[float, float, float]:
        return (row["score"], row["sequence_ratio"], row["token_dice"])

    def add(self, row: dict[str, Any]) -> None:
        if self.limit <= 0:
            return
        if len(self.rows) == self.limit and self._rank(row) <= self._rank(self.rows[-1]):
            return
        self.rows.append(row)
        self.rows.sort(key=self._rank, reverse=True)
        del self.rows[self.limit :]


def _same_path(first: Path, second: Path) -> bool:
    """Compare normalized paths and, when present, filesystem identities."""
    try:
        first_resolved = first.expanduser().resolve(strict=False)
        second_resolved = second.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Unable to resolve paths safely") from exc
    if first_resolved == second_resolved:
        return True
    try:
        return first_resolved.exists() and second_resolved.exists() and first_resolved.samefile(second_resolved)
    except OSError as exc:
        raise ValueError("Unable to compare existing paths safely") from exc


def _validate_cli_paths(args: argparse.Namespace) -> None:
    paths = [("profiles input", Path(args.profiles))]
    paths.extend(
        [
            ("observations output", Path(args.output)),
            ("connector report output", Path(args.report)),
        ]
    )
    if args.website_candidates_output:
        paths.append(("website candidates output", Path(args.website_candidates_output)))
    if args.identity_probes_report:
        paths.append(("identity probe report", Path(args.identity_probes_report)))
    for index, (first_label, first_path) in enumerate(paths):
        for second_label, second_path in paths[index + 1 :]:
            try:
                collision = _same_path(first_path, second_path)
            except ValueError as exc:
                raise ValueError(f"Cannot validate {first_label} and {second_label} paths safely") from exc
            if collision:
                raise ValueError(f"{first_label} and {second_label} resolve to the same file")


def _diagnostic_identity_result(
    profile: dict[str, Any],
    latest_feed_item: dict[str, Any],
    detail_payload: dict[str, Any],
) -> dict[str, Any]:
    """Classify NAV's employer number for a diagnostic probe without building evidence."""
    feed_meta = latest_feed_item.get("_feed_entry") if isinstance(latest_feed_item.get("_feed_entry"), dict) else {}
    body = _detail_body(detail_payload)
    employer = body.get("employer") if isinstance(body.get("employer"), dict) else {}
    statuses = [
        str(feed_meta.get("status") or latest_feed_item.get("status") or "").upper(),
        str(detail_payload.get("status") or "").upper(),
        str(body.get("status") or "").upper(),
    ]
    known_statuses = [status for status in statuses if status]
    if not known_statuses or any(status != "ACTIVE" for status in known_statuses):
        return {
            "result": "detail_status_rejected",
            "detail_status": next((status for status in statuses[1:] if status), "MISSING"),
            "exact_identity_match": False,
        }

    observed_org = digits(
        employer.get("orgnr")
        or employer.get("organisation_number")
        or employer.get("organisationNumber")
    )
    if len(observed_org) != 9:
        return {
            "result": "missing_or_invalid_employer_organisation_number",
            "observed_employer_organisation_number": observed_org or None,
            "exact_identity_match": False,
        }

    entity = registered_employer_entities(profile).get(observed_org)
    if entity is None:
        return {
            "result": "unrelated_organisation_number",
            "observed_employer_organisation_number": observed_org,
            "exact_identity_match": False,
        }
    if entity.get("relationship") == "parent_entity":
        return {
            "result": "exact_parent_organisation_number",
            "observed_employer_organisation_number": observed_org,
            "employer_relationship": "parent_entity",
            "exact_identity_match": True,
        }
    return {
        "result": "brreg_registered_subunit_match",
        "observed_employer_organisation_number": observed_org,
        "employer_relationship": "registered_subunit",
        "exact_identity_match": True,
    }


def run_connector(
    profiles: list[dict[str, Any]],
    *,
    token_env: str = "NAV_JOB_FEED_TOKEN",
    experimental_public_token: bool = False,
    diagnostic_identity_probes: int = 0,
    lookback_days: int = 180,
    max_pages: int = 100,
    max_details: int = 500,
    timeout: float = 20.0,
    min_interval: float = 0.15,
    now: datetime | None = None,
    identity_probe_report_out: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not 1 <= lookback_days <= 185:
        raise ValueError("lookback_days must be between 1 and 185; active NAV ads are time-bounded")
    if max_pages < 1 or max_details < 1:
        raise ValueError("max_pages and max_details must be positive")
    if not 0 <= diagnostic_identity_probes <= 5:
        raise ValueError("diagnostic_identity_probes must be between 0 and 5")
    if diagnostic_identity_probes and identity_probe_report_out is None:
        raise ValueError("identity_probe_report_out is required when diagnostic probes are enabled")
    if diagnostic_identity_probes:
        import os

        if not os.environ.get(token_env, "").strip():
            raise RuntimeError("Diagnostic identity probes require a private NAV consumer token")

    counter = [0]
    token, used_public_token = _load_token(
        env_name=token_env,
        experimental_public_token=experimental_public_token,
        timeout=timeout,
        counter=counter,
    )
    if diagnostic_identity_probes and used_public_token:
        raise RuntimeError("Diagnostic identity probes are disabled for the rotating public token")
    auth = {"Authorization": f"Bearer {token}"}
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = now_utc - timedelta(days=lookback_days)
    since_header = email.utils.format_datetime(since, usegmt=True)

    pages = 0
    truncated = False
    all_items: list[dict[str, Any]] = []
    next_url: str | None = FEED_URL
    last_error: str | None = None

    while next_url and pages < max_pages:
        try:
            raw = _http_get(next_url, {**auth, "If-Modified-Since": since_header}, timeout, counter)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("NAV feed page was not a JSON object")
            items = payload.get("items") or []
            if not isinstance(items, list):
                raise ValueError("NAV feed page items was not a list")
            all_items.extend(item for item in items if isinstance(item, dict))
            pages += 1
            raw_next = payload.get("next_url")
            if raw_next:
                next_url = urllib.parse.urljoin(FEED_BASE, str(raw_next))
                parsed_next = urllib.parse.urlparse(next_url)
                if parsed_next.hostname != "pam-stilling-feed.nav.no" or parsed_next.scheme != "https":
                    raise ValueError("NAV feed returned a next_url outside the approved host")
            else:
                next_url = None
            if next_url and min_interval:
                time.sleep(min_interval)
        except RECOVERABLE_REQUEST_ERRORS as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            break
    if next_url:
        truncated = True

    # Keep the newest event for each ad, but retain earlier employer names as
    # discovery hints: NAV may mask the employer field in inactive updates.
    history: dict[str, list[dict[str, Any]]] = {}
    for item in all_items:
        ad_id = _feed_id(item)
        if ad_id:
            history.setdefault(ad_id, []).append(item)
    latest_items = [max(events, key=_event_datetime) for events in history.values()]

    observations: dict[str, dict[str, Any]] = {}
    website_candidates: dict[str, dict[str, Any]] = {}
    # Review-only name comparisons keyed by (NAV ad ID, internal profile name).
    # The separate capped probe path may fetch detail only after explicit opt-in.
    near_match_candidates = _BoundedCandidateRows(NEAR_MATCH_RETAIN_LIMIT)
    near_match_candidates_scored = 0
    # Explicitly requested identity probes are separate from job observations.
    # Store only feed items and profile references in memory until the capped
    # diagnostic requests run; probe details are never passed to publishers.
    identity_probe_candidates: dict[str, dict[str, Any]] = {}
    detail_requests = 0
    detail_errors = 0
    # Diagnostics measure the strict pre-filter and a looser comparison
    # baseline. The latter can trigger only an explicit capped identity probe.
    name_filter_diagnostics = {
        "unique_ads_with_business_name": 0,
        "unique_ads_with_root_employer_name": 0,
        "unique_ads_with_title_only": 0,
        "unique_ads_without_any_name": 0,
        "candidate_name_comparisons": 0,
        "strict_name_candidate_ads": 0,
        "strict_name_candidate_profile_pairs": 0,
        "loose_candidate_ads_diagnostic_only": 0,
        "loose_candidate_profile_pairs_diagnostic_only": 0,
        "strict_candidates_with_inactive_latest_status": 0,
        "strict_candidates_with_active_latest_status": 0,
        "strict_candidates_with_missing_latest_status": 0,
    }
    latest_status_counts: dict[str, int] = {}
    for item in latest_items:
        item_meta = item.get("_feed_entry") if isinstance(item.get("_feed_entry"), dict) else {}
        item_status = str(item_meta.get("status") or item.get("status") or "MISSING").upper()
        latest_status_counts[item_status] = latest_status_counts.get(item_status, 0) + 1

    for latest in latest_items:
        ad_id = _feed_id(latest)
        events = history.get(ad_id, [latest])
        business_names = []
        root_employer_names = []
        title_names = []
        for event in events:
            event_meta = event.get("_feed_entry") if isinstance(event.get("_feed_entry"), dict) else {}
            if str(event_meta.get("businessName") or "").strip():
                business_names.append(str(event_meta.get("businessName")).strip())
            if str(event.get("employer_name") or "").strip():
                root_employer_names.append(str(event.get("employer_name")).strip())
            title_value = event_meta.get("title") or event.get("title")
            if str(title_value or "").strip():
                title_names.append(str(title_value).strip())

        if business_names:
            name_filter_diagnostics["unique_ads_with_business_name"] += 1
        if root_employer_names:
            name_filter_diagnostics["unique_ads_with_root_employer_name"] += 1
        if not business_names and not root_employer_names and title_names:
            name_filter_diagnostics["unique_ads_with_title_only"] += 1
        if not business_names and not root_employer_names and not title_names:
            name_filter_diagnostics["unique_ads_without_any_name"] += 1

        # Only employer/business names take part in identity pre-filtering.
        # Vacancy titles remain counted as feed metadata but are excluded here.
        feed_names = list(dict.fromkeys(name for event in events for name in _candidate_names(event)))
        candidate_profiles = []
        loose_candidate_profiles = []
        latest_meta = latest.get("_feed_entry") if isinstance(latest.get("_feed_entry"), dict) else {}
        latest_status = str(latest_meta.get("status") or latest.get("status") or "MISSING").upper()
        for profile in profiles:
            profile_names = profile_employer_names(profile)
            name_filter_diagnostics["candidate_name_comparisons"] += len(profile_names) * len(feed_names)
            strict_match = False
            loose_match = False
            best_pair: dict[str, Any] | None = None
            for profile_name in profile_names:
                for feed_name in feed_names:
                    similarity = employer_name_similarity(profile_name, feed_name)
                    if is_name_candidate(profile_name, feed_name):
                        strict_match = True
                    # Symmetric token Dice avoids the old short-name inflation
                    # caused by dividing overlap by the smaller token set.
                    if similarity["token_dice"] >= 0.50 or similarity["sequence_ratio"] >= 0.65:
                        loose_match = True
                    if similarity["score"] >= 0.40 and (
                        best_pair is None or similarity["score"] > best_pair["score"]
                    ):
                        best_pair = {
                            "profile_name": profile_name,
                            "feed_employer_name": feed_name,
                            "score": round(similarity["score"], 4),
                            "token_dice": round(similarity["token_dice"], 4),
                            "sequence_ratio": round(similarity["sequence_ratio"], 4),
                        }
            if strict_match:
                candidate_profiles.append(profile)
            if loose_match:
                loose_candidate_profiles.append(profile)
            if best_pair is not None:
                review_row = {
                    "nav_ad_id": ad_id,
                    "latest_status": latest_status,
                    "latest_modified_at": _event_datetime(latest).isoformat(),
                    **best_pair,
                    "strict_name_match": strict_match,
                    "publication_authorised": False,
                }
                near_match_candidates_scored += 1
                near_match_candidates.add(review_row)
                profile_org = digits(profile.get("organisation_number"))
                if (
                    diagnostic_identity_probes
                    and latest_status == "ACTIVE"
                    and not strict_match
                    and loose_match
                    and len(profile_org) == 9
                ):
                    probe_candidate = {
                        "nav_ad_id": ad_id,
                        "feed_item": latest,
                        "profile": profile,
                        "profile_organisation_number": profile_org,
                        "score": review_row["score"],
                    }
                    previous_probe = identity_probe_candidates.get(ad_id)
                    if previous_probe is None or (
                        probe_candidate["score"],
                        probe_candidate["profile_organisation_number"],
                    ) > (
                        previous_probe["score"],
                        previous_probe["profile_organisation_number"],
                    ):
                        identity_probe_candidates[ad_id] = probe_candidate

        if candidate_profiles:
            name_filter_diagnostics["strict_name_candidate_ads"] += 1
            name_filter_diagnostics["strict_name_candidate_profile_pairs"] += len(candidate_profiles)
        if loose_candidate_profiles:
            name_filter_diagnostics["loose_candidate_ads_diagnostic_only"] += 1
            name_filter_diagnostics["loose_candidate_profile_pairs_diagnostic_only"] += len(loose_candidate_profiles)
        if not candidate_profiles:
            continue
        meta = latest.get("_feed_entry") if isinstance(latest.get("_feed_entry"), dict) else {}
        status = str(meta.get("status") or latest.get("status") or "").upper()
        if status and status != "ACTIVE":
            name_filter_diagnostics["strict_candidates_with_inactive_latest_status"] += 1
            continue
        if status == "ACTIVE":
            name_filter_diagnostics["strict_candidates_with_active_latest_status"] += 1
        else:
            name_filter_diagnostics["strict_candidates_with_missing_latest_status"] += 1
        if detail_requests >= max_details:
            truncated = True
            continue
        detail_url = str(latest.get("url") or f"/api/v1/feedentry/{urllib.parse.quote(ad_id, safe='')}")
        detail_url = urllib.parse.urljoin(FEED_BASE, detail_url)
        try:
            detail_requests += 1
            raw_detail = _http_get(detail_url, auth, timeout, counter)
            detail_payload = json.loads(raw_detail.decode("utf-8"))
            if not isinstance(detail_payload, dict):
                raise ValueError("NAV detail response was not a JSON object")
        except RECOVERABLE_REQUEST_ERRORS as exc:
            detail_errors += 1
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue

        for profile in candidate_profiles:
            observed = build_job_observation(
                profile,
                latest,
                detail_payload,
                raw_detail,
                retrieved_at=now_utc.isoformat().replace("+00:00", "Z"),
                experimental_token=used_public_token,
                now=now_utc,
            )
            if observed:
                observations[observed["id"]] = observed
                candidate = build_website_candidate(
                    profile,
                    observed,
                    detail_payload,
                    raw_detail,
                    retrieved_at=now_utc.isoformat().replace("+00:00", "Z"),
                    experimental_token=used_public_token,
                )
                if candidate:
                    website_candidates[candidate["id"]] = candidate

    selected_probe_candidates = sorted(
        identity_probe_candidates.values(),
        key=lambda item: (-item["score"], item["nav_ad_id"], item["profile_organisation_number"]),
    )[:diagnostic_identity_probes]
    identity_probe_report = {
        "connector": "nav_diagnostic_identity_probes_v1",
        "enabled": diagnostic_identity_probes > 0,
        "requested": diagnostic_identity_probes,
        "hard_max_unique_ads": 5,
        "eligible_unique_ads": len(identity_probe_candidates),
        "selected_unique_ads": len(selected_probe_candidates),
        "probe_requests": 0,
        "skipped_due_detail_budget": 0,
        "results": [],
        "claim_boundary": "Diagnostic only. Similarity never authorises publication; probe results are not job observations or website candidates.",
        "privacy": "Raw NAV responses, applicant data and contact information are not written to this report.",
    }
    for index, candidate in enumerate(selected_probe_candidates):
        if detail_requests >= max_details:
            truncated = True
            identity_probe_report["skipped_due_detail_budget"] = len(selected_probe_candidates) - index
            break
        latest = candidate["feed_item"]
        ad_id = candidate["nav_ad_id"]
        detail_url = str(latest.get("url") or f"/api/v1/feedentry/{urllib.parse.quote(ad_id, safe='')}")
        detail_url = urllib.parse.urljoin(FEED_BASE, detail_url)
        result_row = {
            "nav_ad_id": ad_id,
            "profile_organisation_number": candidate["profile_organisation_number"],
            "similarity_score": candidate["score"],
            "strict_name_match": False,
        }
        detail_requests += 1
        identity_probe_report["probe_requests"] += 1
        try:
            raw_detail = _http_get(detail_url, auth, timeout, counter)
            detail_payload = json.loads(raw_detail.decode("utf-8"))
            if not isinstance(detail_payload, dict):
                raise ValueError("NAV detail response was not a JSON object")
        except RECOVERABLE_REQUEST_ERRORS as exc:
            detail_errors += 1
            result_row.update({
                "result": "detail_fetch_error",
                "error_type": type(exc).__name__,
                "error": "NAV detail request or response could not be processed safely",
                "exact_identity_match": False,
            })
        else:
            result_row.update(
                _diagnostic_identity_result(candidate["profile"], latest, detail_payload)
            )
        identity_probe_report["results"].append(result_row)
        if min_interval and index + 1 < len(selected_probe_candidates):
            time.sleep(min_interval)

    rows = sorted(observations.values(), key=lambda item: (item["organisation_number"], item["published_at"], item["id"]))
    candidate_rows = sorted(website_candidates.values(), key=lambda item: (item["organisation_number"], item["candidate_url"]))
    report = {
        "connector": "nav_official_job_feed_exact_orgnr_v1",
        "profiles_in": len(profiles),
        "feed_pages": pages,
        "feed_items_seen": len(all_items),
        "unique_ads_seen": len(history),
        "detail_requests": detail_requests,
        "detail_errors": detail_errors,
        "latest_feed_status_counts": dict(sorted(latest_status_counts.items())),
        "name_filter_diagnostics": name_filter_diagnostics,
        "name_match_policy": "Existing strict threshold unchanged: token coverage >=0.90 or sequence similarity >=0.90. Employer/business names only; vacancy titles are excluded. Near matches are not fetched by default. Explicit private-token diagnostic probes may fetch at most five ACTIVE unique ads; their outcomes are separate and never published.",
        "near_match_review": {
            "purpose": "Manual triage only; explicit diagnostic probes remain separate, and exact NAV employer organisation number plus registered parent/subunit checks remain mandatory for publication.",
            "employer_names_only": True,
            "vacancy_titles_excluded": True,
            "minimum_score_included": 0.40,
            "candidates_scored": near_match_candidates_scored,
            "retained_candidate_limit": NEAR_MATCH_RETAIN_LIMIT,
            "top_candidates": list(near_match_candidates.rows),
        },
        "diagnostic_identity_probes_requested": diagnostic_identity_probes,
        "diagnostic_identity_probes_attempted": identity_probe_report["probe_requests"],
        "diagnostic_identity_probes_skipped_due_detail_budget": identity_probe_report["skipped_due_detail_budget"],
        "observations": len(rows),
        "companies_with_verified_jobs": len({item["organisation_number"] for item in rows}),
        "employer_homepage_candidates": len(candidate_rows),
        "homepage_candidates_publishable_for_verification": sum(item["rights_status"] == "approved" for item in candidate_rows),
        "homepage_candidates_quarantined": sum(item["rights_status"] != "approved" for item in candidate_rows),
        "requests_total_including_token": counter[0],
        "lookback_days": lookback_days,
        "truncated": truncated,
        "token_mode": "rotating_public_experiment" if used_public_token else "private_consumer_token",
        "publication_status": "quarantined_experiment" if used_public_token else "official_api_terms_require_current_status_updates",
        "error": last_error,
        "privacy": "No applicant/contact personal data is retained in output.",
        "claim_boundary": "Only active, non-expired ads with exact NAV employer organisation number and a valid publication date are emitted.",
    }
    if identity_probe_report_out is not None:
        identity_probe_report_out.clear()
        identity_probe_report_out.update(identity_probe_report)
    return rows, candidate_rows, report


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Find current NAV job ads and verify employer by exact organisation number.")
    parser.add_argument("--profiles", required=True, help="Internal company profile JSONL")
    parser.add_argument("--output", required=True, help="Output external observations JSONL")
    parser.add_argument("--report", required=True, help="Machine-readable connector report JSON")
    parser.add_argument("--website-candidates-output", help="Optional JSONL of NAV employer homepage candidates; not identity-verified")
    parser.add_argument("--token-env", default="NAV_JOB_FEED_TOKEN")
    parser.add_argument("--experimental-public-token", action="store_true", help="Fetch NAV's rotating experiment token; output remains quarantined")
    parser.add_argument("--lookback-days", type=int, default=180)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--max-details", type=int, default=500)
    parser.add_argument(
        "--diagnostic-identity-probes",
        type=int,
        default=0,
        help="Opt in to at most five private-token, diagnostic-only employer identity checks",
    )
    parser.add_argument(
        "--identity-probes-report",
        help="Separate JSON output for diagnostic identity probe outcomes; required when probes are enabled",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--min-interval", type=float, default=0.15)
    args = parser.parse_args()
    if args.diagnostic_identity_probes and not args.identity_probes_report:
        parser.error("--identity-probes-report is required when --diagnostic-identity-probes is enabled")
    if not 0 <= args.diagnostic_identity_probes <= 5:
        parser.error("--diagnostic-identity-probes must be between 0 and 5")
    try:
        _validate_cli_paths(args)
    except ValueError as exc:
        parser.error(str(exc))
    profiles = read_jsonl(Path(args.profiles))
    identity_probe_report: dict[str, Any] = {}
    observations, website_candidates, report = run_connector(
        profiles,
        token_env=args.token_env,
        experimental_public_token=args.experimental_public_token,
        diagnostic_identity_probes=args.diagnostic_identity_probes,
        lookback_days=args.lookback_days,
        max_pages=args.max_pages,
        max_details=args.max_details,
        timeout=args.timeout,
        min_interval=args.min_interval,
        identity_probe_report_out=identity_probe_report,
    )
    write_jsonl(Path(args.output), observations)
    if args.website_candidates_output:
        write_jsonl(Path(args.website_candidates_output), website_candidates)
    if args.identity_probes_report:
        probe_report_path = Path(args.identity_probes_report)
        probe_report_path.parent.mkdir(parents=True, exist_ok=True)
        probe_report_path.write_text(json.dumps(identity_probe_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
