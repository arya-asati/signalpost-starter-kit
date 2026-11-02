from __future__ import annotations

import ipaddress
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlparse


PLATFORMS = {
    "company_site",
    "google_places",
    "linkedin",
    "facebook",
    "instagram",
    "x",
    "youtube",
    "tiktok",
    "glassdoor",
    "indeed",
    "job_board",
    "news",
    "openstreetmap",
    "brreg",
    "apple_app_store",
    "google_play",
    "wikidata",
    "wikipedia",
    "company_directory",
}

SIGNAL_TYPES = {
    "company_profile",
    "profile_handle",
    "profile_metrics",
    "place_summary",
    "review",
    "review_summary",
    "job_posting",
    "workforce_snapshot",
    "public_post",
    "public_mention",
    "buzz_metrics",
}

# “Experimental” means the connector can be benchmarked locally, but its output cannot be
# published or earn competition points until the organiser accepts its rights/reliability path.
PUBLISHABLE_ACQUISITION_MODES = {
    "official_api",
    "licensed_api",
    "company_authorized_export",
    "permitted_public_page",
}
EXPERIMENTAL_ACQUISITION_MODES = {
    "jobspy_experiment",
    "unofficial_api_experiment",
    "rights_review_experiment",
}
INDEPENDENT_SENTIMENT_CLASSES = {"customer_review", "employee_review", "licensed_news", "public_news", "public_mention"}
INDEPENDENT_MENTION_CLASSES = {"licensed_news", "public_news", "public_mention"}

_APPROVED_RIGHTS = frozenset({"approved"})
_PUBLIC_PAGE_MODES = frozenset({"permitted_public_page", "company_authorized_export", "licensed_api", "official_api"})
_NEWS_MODES = frozenset({"permitted_public_page", "company_authorized_export", "licensed_api"})
_SOCIAL_HOSTS = {
    "facebook": ("facebook.com",),
    "instagram": ("instagram.com",),
    "x": ("x.com", "twitter.com"),
    "tiktok": ("tiktok.com",),
}
MAX_PUBLIC_ACTIVITY_AGE_DAYS = 365
MAX_JOB_AGE_DAYS = 365
MAX_SNAPSHOT_AGE_DAYS = 365


def _policy(
    ownership: str,
    identity_rule: str,
    *,
    hosts: tuple[str, ...] = (),
    acquisition_modes: frozenset[str] = _PUBLIC_PAGE_MODES,
) -> dict[str, Any]:
    return {
        "ownership": ownership,
        "identity_rule": identity_rule,
        "hosts": hosts,
        "acquisition_modes": acquisition_modes,
        "rights_statuses": _APPROVED_RIGHTS,
    }


# A missing tuple is deliberately non-publishable. This table binds source ownership,
# class, signal type, identity proof, acquisition mode, and rights status in one place.
SOURCE_POLICIES: dict[tuple[str, str, str], dict[str, Any]] = {
    ("company_site", "company_site", "profile_metrics"): _policy(
        "company_owned", "company_site"
    ),
    ("company_site", "company_site", "public_post"): _policy(
        "company_owned", "company_site"
    ),
    ("job_board", "official_nav_job_feed", "job_posting"): _policy(
        "hiring", "nav_employer", hosts=("arbeidsplassen.nav.no",),
        acquisition_modes=frozenset({"official_api"}),
    ),
    ("linkedin", "professional_network", "profile_handle"): _policy(
        "company_owned", "linkedin_profile", hosts=("linkedin.com",)
    ),
    ("linkedin", "professional_network", "profile_metrics"): _policy(
        "company_owned", "linkedin_profile", hosts=("linkedin.com",)
    ),
    ("linkedin", "professional_network", "workforce_snapshot"): _policy(
        "company_owned", "linkedin_profile", hosts=("linkedin.com",)
    ),
    ("linkedin", "professional_network", "public_post"): _policy(
        "company_owned", "linkedin_profile", hosts=("linkedin.com",)
    ),
    ("linkedin", "job_board", "job_posting"): _policy(
        "hiring", "linkedin_job", hosts=("linkedin.com",)
    ),
    ("youtube", "company_social", "profile_handle"): _policy(
        "company_owned", "youtube_channel", hosts=("youtube.com", "youtu.be")
    ),
    ("youtube", "company_social", "profile_metrics"): _policy(
        "company_owned", "youtube_channel", hosts=("youtube.com", "youtu.be")
    ),
    ("youtube", "company_social", "public_post"): _policy(
        "company_owned", "youtube_channel", hosts=("youtube.com", "youtu.be")
    ),
    ("news", "public_news", "public_mention"): _policy(
        "independent", "exact_legal_name", acquisition_modes=_NEWS_MODES
    ),
    ("news", "licensed_news", "public_mention"): _policy(
        "independent", "exact_legal_name", acquisition_modes=_NEWS_MODES
    ),
    ("news", "public_mention", "public_mention"): _policy(
        "independent", "exact_legal_name", acquisition_modes=_NEWS_MODES
    ),
    ("google_places", "public_business_listing", "place_summary"): _policy(
        "third_party", "google_places", hosts=("google.com", "googleapis.com")
    ),
    ("google_places", "public_business_listing", "company_profile"): _policy(
        "third_party", "google_places", hosts=("google.com", "googleapis.com")
    ),
    ("google_places", "public_business_listing", "profile_metrics"): _policy(
        "third_party", "google_places", hosts=("google.com", "googleapis.com")
    ),
    ("google_places", "customer_review_summary", "review_summary"): _policy(
        "third_party", "google_places", hosts=("google.com", "googleapis.com")
    ),
    ("google_places", "customer_review_summary", "buzz_metrics"): _policy(
        "third_party", "google_places", hosts=("google.com", "googleapis.com")
    ),
    ("google_places", "customer_review", "review"): _policy(
        "third_party", "google_places", hosts=("google.com", "googleapis.com")
    ),
    ("indeed", "job_board", "job_posting"): _policy(
        "hiring", "indeed_job", hosts=("indeed.com",)
    ),
    ("wikidata", "open_knowledge_graph", "company_profile"): _policy(
        "third_party", "wikidata_org_number", hosts=("wikidata.org",)
    ),
}

# Other company social profiles can be published only with a proof object specific to
# that platform. No cross-platform or generic organisation-number proof is accepted.
for _social_platform, _social_hosts in _SOCIAL_HOSTS.items():
    for _signal_type in ("profile_handle", "profile_metrics", "public_post"):
        SOURCE_POLICIES[(_social_platform, "company_social", _signal_type)] = _policy(
            "company_owned", "platform_social_profile", hosts=_social_hosts
        )


def source_policy(item: dict[str, Any]) -> dict[str, Any] | None:
    """Return the exact platform/class/signal policy, if publication is supported."""
    return SOURCE_POLICIES.get((
        str(item.get("platform") or ""),
        str(item.get("source_class") or ""),
        str(item.get("signal_type") or ""),
    ))


def observation_ownership(item: dict[str, Any]) -> str:
    policy = source_policy(item)
    return str(policy.get("ownership")) if policy else "unsupported"


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").casefold().removeprefix("www.")


def _observation_datetime(value: Any) -> datetime | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.max, tzinfo=timezone.utc)
    if not isinstance(value, (str, datetime)):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _identity_proofs(item: dict[str, Any]) -> list[dict[str, Any]]:
    proof = item.get("identity_proof")
    if not isinstance(proof, list):
        return []
    return [
        entry for entry in proof
        if isinstance(entry, dict)
        and str(entry.get("type") or "").strip()
        and any(value not in (None, "", [], {}) for key, value in entry.items() if key != "type")
    ]


def _proof_value(proof: dict[str, Any]) -> dict[str, Any]:
    value = proof.get("value")
    return value if isinstance(value, dict) else {}


def _normalised_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalised_name_core(value: Any) -> str:
    name = re.sub(r"[^a-z0-9]+", " ", _normalised_name(value).translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"})))
    return re.sub(r"\s+(as|asa|enk|da|ans|ba|sa)$", "", name).strip()


def _valid_http_url(value: str) -> tuple[bool, Any]:
    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return False, None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname or ""
        valid_host = bool(host) and not any(char in host for char in "%_\\")
        if valid_host and ("[" in parsed.netloc or "]" in parsed.netloc or ":" in host):
            valid_host = ipaddress.ip_address(host).version == 6
        elif valid_host:
            labels = host.rstrip(".").split(".")
            valid_host = all(
                label and len(label) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                for label in labels
            ) and not host.endswith(".")
        valid = (
            parsed.scheme.casefold() in {"http", "https"}
            and valid_host
            and parsed.username is None
            and parsed.password is None
            and port in {None, 80 if parsed.scheme.casefold() == "http" else 443}
        )
        return bool(valid), parsed
    except (ValueError, UnicodeError):
        return False, None


def _host_matches_policy(host: str, allowed: tuple[str, ...]) -> bool:
    host = host.casefold().removeprefix("www.")
    return any(host == candidate or host.endswith("." + candidate) for candidate in allowed)


def _linkedin_company_key(url: str) -> str | None:
    valid, parsed = _valid_http_url(url)
    if not valid or not _host_matches_policy(parsed.hostname or "", ("linkedin.com",)):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].casefold() != "company":
        return None
    return parts[1].casefold()


def _identity_matches_context(
    item: dict[str, Any],
    proofs: list[dict[str, Any]],
    expected_organisation_number: str | None,
    expected_company_name: str | None,
    expected_website_url: str | None,
    identity_rule: str,
) -> list[str]:
    reasons: list[str] = []
    platform = str(item.get("platform") or "")
    source_class = str(item.get("source_class") or "")
    signal_type = str(item.get("signal_type") or "")
    org = str(item.get("organisation_number") or "")

    if expected_organisation_number and org != expected_organisation_number:
        reasons.append("organisation number does not match the profile")

    if source_class == "official_nav_job_feed":
        nav = next((proof for proof in proofs if proof.get("type") == "nav_employer_organisation_number"), None)
        if nav is None:
            reasons.append("missing NAV employer organisation-number proof")
            return reasons
        parent = str(nav.get("expected_parent_organisation_number") or nav.get("expected_organisation_number") or "")
        employer = str(nav.get("observed_employer_organisation_number") or "")
        expected = expected_organisation_number or parent
        match = str(nav.get("match") or "")
        if not expected or parent != expected:
            reasons.append("NAV parent organisation number does not match the profile")
        if match == "exact":
            if employer != expected:
                reasons.append("NAV employer organisation number is not an exact parent match")
        elif match == "exact_registered_subunit":
            subunit = next((proof for proof in proofs if proof.get("type") == "brreg_registered_subunit_relationship"), None)
            if subunit is None:
                reasons.append("NAV subunit match lacks Brønnøysund relationship proof")
            else:
                if str(subunit.get("parent_organisation_number") or "") != expected:
                    reasons.append("Brønnøysund parent organisation number does not match the profile")
                if str(subunit.get("subunit_organisation_number") or "") != employer:
                    reasons.append("Brønnøysund subunit number does not match the NAV employer")
                if subunit.get("match") != "exact_parent_child":
                    reasons.append("Brønnøysund parent/subunit relationship is not verified")
                subunit_url = urlparse(str(subunit.get("source_url") or ""))
                try:
                    subunit_port = subunit_url.port
                    subunit_query = parse_qs(subunit_url.query, keep_blank_values=True)
                except ValueError:
                    subunit_port = -1
                    subunit_query = {}
                if (
                    subunit_url.scheme.casefold() != "https"
                    or (subunit_url.hostname or "").casefold() != "data.brreg.no"
                    or subunit_port not in {None, 443}
                    or subunit_url.username is not None
                    or subunit_url.password is not None
                    or subunit_url.path != "/enhetsregisteret/api/underenheter"
                    or subunit_query.get("overordnetEnhet") != [expected]
                ):
                    reasons.append("Brønnøysund subunit proof lacks exact endpoint and parent-query provenance")
                if not re.fullmatch(r"[0-9a-fA-F]{64}", str(subunit.get("content_sha256") or "")):
                    reasons.append("Brønnøysund subunit proof lacks a valid content hash")
                if _observation_datetime(subunit.get("retrieved_at")) is None:
                    reasons.append("Brønnøysund subunit proof lacks a valid retrieval timestamp")
        else:
            reasons.append("NAV employer match is neither exact parent nor verified subunit")
        return reasons

    if identity_rule == "indeed_job":
        employer = next((proof for proof in proofs if proof.get("type") == "indeed_employer_organisation_number"), None)
        if employer is None or str(employer.get("observed_employer_organisation_number") or "") != org:
            reasons.append("Indeed job lacks an exact employer organisation-number match")

    if identity_rule == "wikidata_org_number":
        wikidata = next((proof for proof in proofs if proof.get("type") == "wikidata_organisation_number_match"), None)
        if wikidata is None or str(wikidata.get("value") or "") != org:
            reasons.append("Wikidata profile lacks an exact organisation-number match")

    if platform == "company_site":
        gate = next((proof for proof in proofs if proof.get("type") == "website_identity_gate"), None)
        if gate is None or gate.get("status") != "exact":
            reasons.append("company-site observation lacks an exact website identity gate")
        if expected_website_url:
            expected_host = (urlparse(expected_website_url).hostname or "").casefold().removeprefix("www.")
            observed_host = (urlparse(str(item.get("source_url") or "")).hostname or "").casefold().removeprefix("www.")
            related_host = bool(
                expected_host
                and observed_host
                and (
                    observed_host == expected_host
                    or observed_host.endswith("." + expected_host)
                    or expected_host.endswith("." + observed_host)
                )
            )
            if not related_host:
                reasons.append("company-site observation host does not match the registered website")

    if identity_rule == "linkedin_profile":
        linkedin_proof = False
        for proof in proofs:
            value = _proof_value(proof)
            if proof.get("type") == "linkedin_structured_company_identity":
                resolved_key = _linkedin_company_key(str(value.get("resolved_url") or ""))
                source_key = _linkedin_company_key(str(item.get("source_url") or ""))
                # Posts link to the post URL rather than the company page, but
                # they remain tied to the exact profile extracted in this proof.
                post_source = False
                if signal_type == "public_post":
                    post_valid, post_url = _valid_http_url(str(item.get("source_url") or ""))
                    post_source = post_valid and _host_matches_policy(post_url.hostname or "", ("linkedin.com",))
                same_page = bool(resolved_key and (source_key == resolved_key or post_source))
                domain_match = bool(
                    value.get("reverse_domain_match") is True
                    and value.get("official_domain")
                    and value.get("official_domain") == value.get("linkedin_website_domain")
                )
                linkedin_proof = linkedin_proof or (
                    value.get("publishable_candidate") is True
                    and same_page
                    and (value.get("exact_legal_name_core") is True or domain_match)
                )
            elif proof.get("type") == "linkedin_discovery_identity":
                full_name = value.get("full_legal_name_match") is True
                core_name = value.get("legal_name_core_match") is True
                alias = value.get("official_site_alias_match") is True
                domain = value.get("reverse_domain_match") is True
                location = value.get("location_match") is True
                company_id = value.get("company_id_job_link") is True
                leaders = bool(value.get("matched_registered_leaders"))
                linkedin_proof = linkedin_proof or (
                    value.get("exact_entity") is True
                    and ((core_name and domain) or (alias and domain) or (full_name and location)
                         or (full_name and company_id) or (alias and location and leaders))
                )
        if not linkedin_proof:
            reasons.append("LinkedIn observation lacks structured exact-company identity proof")

    if identity_rule == "linkedin_job":
        company_url = next((str(proof.get("value") or "") for proof in proofs if proof.get("type") == "exact_linkedin_company_url_match"), "")
        url_ok, company_parsed = _valid_http_url(company_url)
        has_company_link = any(proof.get("type") in {
            "linkedin_company_id_confirmed_by_exact_job_company_url",
            "job_detail_exact_company_url_match",
        } for proof in proofs)
        if not url_ok or not (company_parsed.hostname or "").casefold().endswith("linkedin.com") or not has_company_link:
            reasons.append("LinkedIn job lacks exact company-URL identity proof")

    if identity_rule == "youtube_channel":
        channel_name_proof = next((proof for proof in proofs if proof.get("type") == "exact_normalized_youtube_channel_name"), None)
        site_identity_proof = next((proof for proof in proofs if proof.get("type") == "company_site_identity"), None)
        proof_value = str((channel_name_proof or {}).get("value") or "")
        proof_legal_name = (channel_name_proof or {}).get("legal_name")
        expected_core = _normalised_name_core(expected_company_name or proof_legal_name)
        observed_core = _normalised_name_core(proof_value)
        if channel_name_proof is None or not expected_core or observed_core != expected_core:
            reasons.append("YouTube observation lacks exact normalized channel-name proof")
        try:
            site_identity_score = float((site_identity_proof or {}).get("score") or 0)
        except (TypeError, ValueError):
            site_identity_score = 0
        if site_identity_score < 0.9:
            reasons.append("YouTube observation lacks strong company-site identity corroboration")

    if identity_rule == "platform_social_profile":
        proof_type = f"{platform}_profile_identity"
        platform_proof = next((proof for proof in proofs if proof.get("type") == proof_type), None)
        value = _proof_value(platform_proof or {})
        if not (
            value.get("exact_entity") is True
            and value.get("normalized_name_match") is True
            and (value.get("official_site_link_match") is True or value.get("reverse_domain_match") is True)
        ):
            reasons.append(f"{platform} observation lacks platform-specific exact-company identity proof")

    if identity_rule == "google_places":
        values = {str(proof.get("type")): proof.get("value") for proof in proofs}
        try:
            name_score = float(values.get("maps_title_name_score") or 0)
            fuzzy_score = float(values.get("fuzzy_name_score") or 0)
        except (TypeError, ValueError):
            name_score = fuzzy_score = 0.0
        independent_match = values.get("independently_verified_trade_name") is True and bool(
            next((proof.get("source_url") for proof in proofs if proof.get("type") == "independently_verified_trade_name"), None)
        )
        corroborations = sum(values.get(key) is True for key in (
            "registry_address_match", "registry_postcode_city_match", "registry_phone_match", "verified_website_domain_match"
        ))
        exact_name = name_score >= 0.99 and corroborations >= 1
        combined = name_score >= 0.66 and corroborations >= 2
        fuzzy_domain = fuzzy_score >= 0.85 and values.get("verified_website_domain_match") is True
        if not (exact_name or combined or fuzzy_domain or independent_match):
            reasons.append("Google Places observation lacks corroborated exact-business identity proof")

    if signal_type == "public_mention":
        if source_class not in INDEPENDENT_MENTION_CLASSES:
            reasons.append("public mention is not classified as independent evidence")
        expected_name = _normalised_name(expected_company_name)
        name_proof = next((
            proof for proof in proofs
            if proof.get("type") == "manual_exact_entity_review"
            and bool(_normalised_name(proof.get("legal_name")))
            and (not expected_name or _normalised_name(proof.get("legal_name")) == expected_name)
        ), None)
        if name_proof is None:
            name_proof = next((
                proof for proof in proofs
                if proof.get("type") == "exact_legal_name_in_news_title"
                and (not expected_name or _normalised_name(proof.get("value")) == expected_name)
            ), None)
        if name_proof is None:
            reasons.append("independent mention lacks exact legal-name identity proof")

    return reasons


def validate_observation(
    item: dict[str, Any],
    *,
    expected_organisation_number: str | None = None,
    expected_company_name: str | None = None,
    expected_website_url: str | None = None,
    as_of: str | datetime | None = None,
) -> list[str]:
    reasons: list[str] = []
    if not isinstance(item, dict):
        return ["observation is not an object"]

    if not str(item.get("id") or "").strip():
        reasons.append("missing observation id")
    organisation_number = str(item.get("organisation_number") or "")
    if not re.fullmatch(r"\d{9}", organisation_number):
        reasons.append("missing or invalid organisation number")
    if expected_organisation_number and organisation_number != str(expected_organisation_number):
        reasons.append("organisation number does not match the profile")
    if item.get("platform") not in PLATFORMS:
        reasons.append("unsupported platform")
    if item.get("signal_type") not in SIGNAL_TYPES:
        reasons.append("unsupported signal type")
    source_url = str(item.get("source_url") or "")
    valid_url, parsed_url = _valid_http_url(source_url)
    if not valid_url:
        reasons.append("missing or invalid source URL")
    policy = source_policy(item)
    if policy is None:
        reasons.append("unsupported platform/source-class/observation-type combination")
    elif policy.get("hosts") and valid_url and not _host_matches_policy(parsed_url.hostname or "", policy["hosts"]):
        reasons.append("observation source host does not match its platform policy")
    retrieved = _observation_datetime(item.get("retrieved_at"))
    now = _observation_datetime(as_of) or datetime.now(timezone.utc)
    if retrieved is None:
        reasons.append("missing or invalid retrieval time")
    elif retrieved > now + timedelta(days=1):
        reasons.append("retrieval time is in the future")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(item.get("content_sha256") or "")):
        reasons.append("missing content hash")
    if item.get("exact_entity") is not True:
        reasons.append("exact legal entity is not verified")
    proofs = _identity_proofs(item)
    if not proofs:
        reasons.append("missing exact-entity proof")
    else:
        reasons.extend(_identity_matches_context(
            item,
            proofs,
            str(expected_organisation_number) if expected_organisation_number else None,
            expected_company_name,
            expected_website_url,
            str(policy.get("identity_rule") or "") if policy else "",
        ))
    if item.get("acquisition_mode") not in PUBLISHABLE_ACQUISITION_MODES:
        reasons.append("acquisition mode is not approved for publication")
    if policy and item.get("acquisition_mode") not in policy["acquisition_modes"]:
        reasons.append("acquisition mode is not allowed for this source policy")
    if item.get("rights_status") != "approved":
        reasons.append("source rights are not approved")
    if policy and item.get("rights_status") not in policy["rights_statuses"]:
        reasons.append("rights status is not allowed for this source policy")
    source_class = str(item.get("source_class") or "").strip()
    if not source_class:
        reasons.append("missing source class")
    if item.get("signal_type") in {"review", "public_post", "public_mention", "job_posting"} and not str(item.get("evidence_span") or "").strip():
        reasons.append("missing evidence span")

    platform = item.get("platform")
    signal_type = item.get("signal_type")
    if source_class == "official_nav_job_feed":
        host = (parsed_url.hostname or "").casefold() if valid_url else ""
        if platform != "job_board" or host != "arbeidsplassen.nav.no":
            reasons.append("official NAV job observation has an unexpected platform or host")

    if signal_type in {"job_posting", "public_post", "public_mention"}:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        published_value = next((item.get(key) for key in ("published_at", "date_published", "date_posted") if item.get(key)), None)
        if published_value is None:
            published_value = next((metrics.get(key) for key in ("published_at", "date_published", "date_posted") if metrics.get(key)), None)
        published = _observation_datetime(published_value)
        if published is None:
            reasons.append("missing or invalid publication date")
        elif published > now:
            reasons.append("publication date is in the future")

        if published is not None and signal_type in {"public_post", "public_mention"}:
            if published < now - timedelta(days=MAX_PUBLIC_ACTIVITY_AGE_DAYS):
                reasons.append("public activity is stale")

        if signal_type == "job_posting":
            status = str(item.get("status") or metrics.get("status") or "").strip().upper()
            active = item.get("active") is True or metrics.get("active") is True
            expiry_value = next((item.get(key) for key in ("expires_at", "expiry_date") if item.get(key)), None)
            if expiry_value is None:
                expiry_value = next((metrics.get(key) for key in ("expires_at", "expiry_date") if metrics.get(key)), None)
            expiry = _observation_datetime(expiry_value)
            if expiry_value and expiry is None:
                reasons.append("invalid job expiry date")
            if status and status != "ACTIVE":
                reasons.append("job posting is not active")
            if expiry is not None and expiry <= now:
                reasons.append("job posting has expired")
            if status != "ACTIVE" and not active and expiry is None:
                reasons.append("job posting lacks current active-status evidence")
            if published is not None and published < now - timedelta(days=MAX_JOB_AGE_DAYS):
                reasons.append("job posting is stale")

    if item.get("sentiment_label") is not None:
        if item.get("sentiment_label") not in {"positive", "neutral", "negative", "mixed"}:
            reasons.append("unsupported sentiment label")
        if item.get("source_class") not in INDEPENDENT_SENTIMENT_CLASSES:
            reasons.append("sentiment source is not independent")
        if not item.get("sentiment_model_version"):
            reasons.append("missing sentiment model version")

    if platform == "brreg":
        reasons.append("official registry facts belong in company context, not external observations")
    if signal_type in {"profile_metrics", "workforce_snapshot"} and retrieved is not None:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        effective = _observation_datetime(
            item.get("effective_at") or item.get("as_of") or metrics.get("effective_at") or metrics.get("as_of")
        ) or retrieved
        if effective > now + timedelta(days=1):
            reasons.append("snapshot effective date is in the future")
        elif effective < now - timedelta(days=MAX_SNAPSHOT_AGE_DAYS):
            reasons.append("profile or workforce snapshot is stale")
    if source_class == "official_nav_job_feed" and item.get("acquisition_mode") != "official_api":
        reasons.append("official NAV observations require the official API acquisition mode")
    if platform == "company_site" and item.get("acquisition_mode") not in {"permitted_public_page", "company_authorized_export"}:
        reasons.append("company-site observations require a permitted first-party acquisition mode")
    return reasons


def publishable_observation(
    item: dict[str, Any],
    *,
    expected_organisation_number: str | None = None,
    expected_company_name: str | None = None,
    expected_website_url: str | None = None,
    as_of: str | datetime | None = None,
) -> bool:
    return not validate_observation(
        item,
        expected_organisation_number=expected_organisation_number,
        expected_company_name=expected_company_name,
        expected_website_url=expected_website_url,
        as_of=as_of,
    )


def _as_datetime(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def aggregate_footprint(
    observations: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    freshness_days: int = 45,
) -> dict[str, Any]:
    """Create a conservative company-level external-footprint summary.

    Counts are kept source-specific. They are not added into a fake universal “popularity” number.
    The normalized buzz and sentiment scores are only emitted when their minimum evidence gates pass.
    """

    accepted = [item for item in observations if publishable_observation(item)]
    rejected = [{"id": item.get("id"), "reasons": validate_observation(item)} for item in observations if not publishable_observation(item)]
    now = _as_datetime(as_of) or datetime.now(timezone.utc)
    cutoff_seconds = freshness_days * 86_400
    fresh = []
    for item in accepted:
        retrieved = _as_datetime(item.get("retrieved_at"))
        if retrieved and 0 <= (now - retrieved).total_seconds() <= cutoff_seconds:
            fresh.append(item)

    by_platform: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in accepted:
        by_platform[item["platform"]].append(item)

    review_items = [item for item in accepted if item["signal_type"] in {"review", "review_summary"}]
    job_items = [item for item in accepted if item["signal_type"] == "job_posting"]
    public_items = [item for item in accepted if item["signal_type"] in {"public_post", "public_mention", "buzz_metrics"}]
    sentiment_items = [item for item in accepted if item.get("sentiment_label")]
    independent_sentiment_hosts = {_host(str(item["source_url"])) for item in sentiment_items}
    independent_sentiment_reviewers = {
        str(item.get("reviewer_id")) for item in sentiment_items if item.get("reviewer_id")
    }

    label_values = {"negative": -1, "neutral": 0, "positive": 1}
    scalar_sentiment = [label_values[item["sentiment_label"]] for item in sentiment_items if item["sentiment_label"] in label_values]
    sentiment_ready = len(sentiment_items) >= 10 and (
        len(independent_sentiment_hosts) >= 2 or len(independent_sentiment_reviewers) >= 10
    )
    sentiment_score = round(50 + 50 * sum(scalar_sentiment) / len(scalar_sentiment), 1) if sentiment_ready and scalar_sentiment else None

    engagement = sum(
        int((item.get("metrics") or {}).get(field) or 0)
        for item in public_items
        for field in ("likes", "comments", "shares")
    )
    unique_public_items = len({str(item.get("source_url")) for item in public_items})

    return {
        "status": "available" if accepted else "not_available",
        "accepted_observations": len(accepted),
        "rejected_observations": len(rejected),
        "rejections": rejected,
        "platforms": sorted(by_platform),
        "platform_counts": {platform: len(items) for platform, items in sorted(by_platform.items())},
        "fresh_observations": len(fresh),
        "freshness_days": freshness_days,
        "review_signal_count": len(review_items),
        "active_job_count": len({str(item.get("source_url")) for item in job_items}),
        "public_item_count": unique_public_items,
        "public_engagement": engagement,
        "sentiment": {
            "status": "available" if sentiment_ready else "abstain",
            "score_0_100": sentiment_score,
            "items": len(sentiment_items),
            "independent_sources": len(independent_sentiment_hosts),
            "independent_reviewers": len(independent_sentiment_reviewers),
            "label_counts": dict(Counter(item["sentiment_label"] for item in sentiment_items)),
            "warning": "Dated contextual signal, not a timeless fact about the company.",
        },
    }
