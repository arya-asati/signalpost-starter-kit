from __future__ import annotations

import re
import unicodedata
import urllib.parse
from datetime import datetime
from typing import Any



LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}

# These terms explicitly indicate an umbrella/group presentation. They are only
# used to stop a subsidiary mention from proving that the homepage belongs to it.
GROUP_CONTEXT_TERMS = {
    "konsern", "konsernet", "group", "groups", "portfolio", "subsidiary",
    "subsidiaries", "companies", "selskaper", "vare", "holdinggroup",
}

# Short generic browser titles should not override a legal name shown in the
# site's company identity text, e.g. a footer that names the operating entity.
GENERIC_TITLE_TERMS = {
    "it", "service", "services", "technology", "technologies", "solutions",
    "solution", "home", "homepage", "frontpage", "front", "page", "welcome",
    "official", "site", "web", "website", "forside", "contact", "kontakt",
    "about", "om", "oss", "company", "companies", "products", "product",
    "business", "professional", "professionalservices",
}

SOCIAL_DOMAINS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
}

ALLOWED_VERIFICATION_METHODS = {
    "official_platform_api",
    "licensed_provider",
    "approved_connector",
    "permitted_public_profile",
}


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(
        str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"})
    )
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC and len(token) > 1]


def _structured_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "legalName", "alternateName"} and isinstance(child, str):
                names.append(child)
            else:
                names.extend(_structured_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_structured_names(child))
    return names


def _normalised_text(value: Any) -> str:
    return " ".join(_tokens(value))


# CARLIN_REGISTRY_OFFICER_IDENTITY_RULE

def _identity_hostname(url: str | None) -> str:
    """Normalize a URL hostname, including registry values without a scheme."""
    raw = str(url or "").strip()
    if not raw:
        return ""

    if "://" not in raw:
        raw = "//" + raw

    try:
        host = (urllib.parse.urlparse(raw).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""

    return host.removeprefix("www.")


def _registry_listed_officer_ownership(
    profile: dict[str, Any],
    website_value: dict[str, Any],
    final_url: str,
    legal_name_tokens: list[str],
) -> bool:
    """
    Verify a narrow first-party identity case.

    Required:
    1. The registry-listed homepage and fetched homepage have the same host.
    2. Official roles contain an active managing director or board chair.
    3. That officer's first and last names appear on the homepage.
    4. The homepage explicitly connects the legal entity name to a
       first-person ownership/operation statement.
    """

    if not legal_name_tokens:
        return False

    registered_url = profile.get("website")
    if not registered_url:
        registry_value = (
            profile.get("evidence", {})
            .get("registry", {})
            .get("value", {})
        )
        if isinstance(registry_value, dict):
            registered_url = (
                registry_value.get("hjemmeside")
                or registry_value.get("website")
            )

    registered_host = _identity_hostname(registered_url)
    fetched_host = _identity_hostname(final_url)

    if not registered_host or registered_host != fetched_host:
        return False

    requested_url = website_value.get("requested_url")
    if requested_url:
        if _identity_hostname(requested_url) != registered_host:
            return False

    homepage_parts = [
        website_value.get("title"),
        website_value.get("description"),
        website_value.get("identity_text_excerpt"),
        website_value.get("main_text_excerpt"),
    ]

    rendered = website_value.get("js_fallback") or {}
    homepage_parts.extend([
        rendered.get("title"),
        rendered.get("main_text_excerpt"),
    ])

    homepage_text = " ".join(
        str(part or "")
        for part in homepage_parts
        if str(part or "").strip()
    )

    homepage_tokens = set(_tokens(homepage_text))
    if not homepage_tokens:
        return False

    # Verify an active, authoritative company officer.
    roles_record = (
        profile.get("evidence", {})
        .get("roles", {})
    )

    if roles_record.get("status") != "available":
        return False

    roles_value = roles_record.get("value") or {}
    role_rows = (
        roles_value.get("roles", [])
        if isinstance(roles_value, dict)
        else []
    )

    eligible_role_labels = {
        "daglig leder",
        "styrets leder",
        "styreleder",
        "managing director",
        "chief executive officer",
        "chairman of the board",
    }

    officer_verified = False

    for role in role_rows:
        if not isinstance(role, dict):
            continue

        inactive_value = role.get("inactive")
        inactive = (
            inactive_value is True
            or str(inactive_value).strip().casefold()
            in {"true", "1", "yes"}
        )

        if inactive:
            continue

        role_label = _normalised_text(
            role.get("role") or role.get("role_code")
        )

        if role_label not in eligible_role_labels:
            continue

        officer_tokens = _tokens(role.get("name"))

        # Require at least a first name and surname.
        # Middle names may legitimately be omitted from the website.
        if len(officer_tokens) < 2:
            continue

        first_and_last = {
            officer_tokens[0],
            officer_tokens[-1],
        }

        if first_and_last.issubset(homepage_tokens):
            officer_verified = True
            break

    if not officer_verified:
        return False

    # Require the company name and explicit first-person ownership
    # language in the same captured homepage sentence.
    ownership_phrases = (
        "driver jeg mitt eget aksjeselskap",
        "jeg driver mitt eget aksjeselskap",
        "driver jeg mitt eget selskap",
        "jeg driver mitt eget selskap",
        "jeg eier selskapet",
        "mitt eget aksjeselskap",
        "i run my own company",
        "run my own company",
        "i own my company",
        "own my own company",
    )

        # HTML extraction can insert newlines inside a sentence.
    # Normalize line breaks before checking sentence-level identity evidence.
    sentence_text = re.sub(r"[\r\n\t]+", " ", homepage_text)

    sentences = re.split(
        r"(?<=[.!?])\s+|;",
        sentence_text,
    )

    for sentence in sentences:
        normalized_sentence = _normalised_text(sentence)
        sentence_tokens = set(_tokens(sentence))

        company_name_present = set(legal_name_tokens).issubset(
            sentence_tokens
        )

        ownership_statement_present = any(
            phrase in normalized_sentence
            for phrase in ownership_phrases
        )

        if company_name_present and ownership_statement_present:
            return True

    return False



def _hostname(url: str | None) -> str:
    try:
        host = (urllib.parse.urlparse(str(url or "")).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _registered_site(url: str | None) -> str:
    # Avoid a runtime/public-suffix download dependency. The host is returned
    # after removing www; subdomain equivalence is checked separately.
    return _hostname(url)


def _same_official_domain(left: str | None, right: str | None) -> bool:
    first = _registered_site(left)
    second = _registered_site(right)
    if not first or not second:
        return False
    return (
        first == second
        or first.endswith("." + second)
        or second.endswith("." + first)
    )


def _social_profile_key(url: str | None) -> tuple[str, str] | None:
    """Canonical platform/path key; query parameters never establish identity."""
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    platform = next(
        (name for domain, name in SOCIAL_DOMAINS.items() if host == domain or host.endswith("." + domain)),
        None,
    )
    if not platform or parsed.scheme not in {"http", "https"}:
        return None
    parts = [urllib.parse.unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
    if not parts:
        return None
    lowered = [part.casefold() for part in parts]
    if platform == "linkedin":
        if lowered[0] != "company" or len(parts) < 2:
            return None
        parts = parts[:2]
    elif platform == "youtube":
        if lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
            return None
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    elif platform == "instagram":
        if lowered[0] in {"p", "reel", "reels", "stories", "explore"}:
            return None
        parts = parts[:1]
    elif platform == "facebook":
        if lowered[0] in {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins", "profile.php"}:
            return None
        parts = parts[:1]
    elif platform == "x":
        if lowered[0] in {"intent", "share", "home", "search", "i"}:
            return None
        parts = parts[:1]
    elif platform == "tiktok":
        if not parts[0].startswith("@"):
            return None
        parts = parts[:1]
    return platform, "/" + "/".join(parts).casefold()


def assess_website_identity(profile: dict[str, Any]) -> dict[str, Any]:
    website = profile.get("evidence", {}).get("website", {})
    value = website.get("value") or {}
    core = _tokens(profile.get("name"))
    final_url = value.get("final_url") or website.get("source_url") or profile.get("website") or ""
    hostname = urllib.parse.urlparse(str(final_url)).hostname or ""
    structured_names = _structured_names(value.get("structured_organisations") or [])
    rendered = value.get("js_fallback") or {}

    # Strong homepage identity material: page metadata, host and organization
    # structured data. A broad body/footer excerpt is tracked separately because
    # group sites often enumerate many subsidiaries in global navigation/footer.
    strong_homepage_parts = [
        value.get("title"), value.get("description"), hostname, *structured_names,
        rendered.get("title"),
    ]
    identity_excerpt = value.get("identity_text_excerpt")
    homepage_identity_parts = [*strong_homepage_parts, identity_excerpt]
    candidate_parts = [
        *homepage_identity_parts,
        value.get("main_text_excerpt"),
        *[page.get("title") for page in value.get("pages", [])],
        *[page.get("main_text_excerpt") for page in value.get("pages", [])],
        *[page.get("identity_text_excerpt") for page in value.get("pages", [])],
        rendered.get("main_text_excerpt"),
    ]
    candidate_text = " ".join(str(part or "") for part in candidate_parts)
    homepage_candidate_text = " ".join(
        str(part or "")
        for part in [*homepage_identity_parts, value.get("main_text_excerpt"), rendered.get("main_text_excerpt")]
    )
    normalized_candidate_text = _normalised_text(candidate_text)
    candidate_tokens = set(_tokens(candidate_text))
    org_digits = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    compact_candidate = re.sub(r"\D", "", candidate_text)
    compact_homepage_candidate = re.sub(r"\D", "", homepage_candidate_text)
    compact_strong_homepage = re.sub(r"\D", "", " ".join(str(part or "") for part in strong_homepage_parts))
    overlap = sorted(set(core) & candidate_tokens)
    ratio = len(overlap) / len(set(core)) if core else 0.0
    reasons: list[str] = []

    parked_markers = (
        "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
        "her flytter snart en ny gjest", "has been informing visitors",
        "find the best information and most relevant links on all topics related to",
    )
    normalized_raw = unicodedata.normalize("NFKD", candidate_text).encode("ascii", "ignore").decode().casefold()
    homepage_token_sets = [set(_tokens(part)) for part in homepage_identity_parts if part]
    strong_token_sets = [set(_tokens(part)) for part in strong_homepage_parts if part]
    exact_homepage_name = bool(core and any(set(core).issubset(tokens) for tokens in homepage_token_sets))
    strong_exact_homepage_name = bool(core and any(set(core).issubset(tokens) for tokens in strong_token_sets))
    substantive_homepage = len(str(value.get("main_text_excerpt") or "").strip()) >= 100
    is_business_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(profile.get("name") or ""), re.I))

    title_text = " ".join(str(part or "") for part in (value.get("title"), rendered.get("title")))
    title_tokens = set(_tokens(title_text))
    meaningful_title_tokens = title_tokens - GENERIC_TITLE_TERMS
    title_conflicts_with_legal_name = bool(
        core and meaningful_title_tokens and not (set(core) & title_tokens) and not strong_exact_homepage_name
    )
    normalized_head = _normalised_text(" ".join(str(part or "") for part in (value.get("title"), value.get("description"))))
    group_branded_homepage = bool(GROUP_CONTEXT_TERMS & set(normalized_head.split()))
    ambiguous_parent_brand = (
        not strong_exact_homepage_name
        and not (org_digits and org_digits in compact_strong_homepage)
        and (group_branded_homepage or (title_conflicts_with_legal_name and bool(identity_excerpt)))
    )

    registry_listed_officer_identity = _registry_listed_officer_ownership(
        profile,
        value,
        str(final_url),
        core,
    )

    if any(marker in normalized_raw for marker in parked_markers):
        score = 0.1
        reasons.append("captured page is a parked, for-sale, or generic hosting placeholder")
    elif is_business_sports_club and "bedriftsidrett" not in normalized_candidate_text and "b i l" not in normalized_candidate_text:
        score = 0.3
        reasons.append("business sports-club entity points to the operating company's site without club evidence")
    elif ambiguous_parent_brand:
        score = 0.55
        reasons.append(
            "homepage presents a group/parent brand or conflicting brand; a subsidiary mention in body/footer text is not exact homepage identity proof"
        )
    elif org_digits and org_digits in compact_homepage_candidate:
        score = 1.0
        reasons.append("exact organisation number appears in homepage identity evidence")
    elif len(core) >= 2 and strong_exact_homepage_name:
        score = 0.95
        reasons.append("all normalized legal-name tokens appear together in strong homepage identity evidence")
    elif len(core) >= 2 and exact_homepage_name:
        score = 0.92
        reasons.append("all normalized legal-name tokens appear in homepage identity text, without a conflicting group-brand signal")
    elif len(core) == 1 and strong_exact_homepage_name and substantive_homepage:
        score = 0.95
        reasons.append("single distinctive legal-name token appears in strong homepage identity evidence with substantive content")
    elif len(core) == 1 and exact_homepage_name and substantive_homepage:
        score = 0.92
        reasons.append("single distinctive legal-name token appears in homepage identity text with substantive content")
    elif registry_listed_officer_identity:
        score = 0.93
        reasons.append(
            "registry-listed domain, active company officer and explicit "
            "first-person legal-entity statement are corroborated on homepage"
        )
    elif ratio >= 0.75 and len(overlap) >= 2:
        score = 0.85
        reasons.append("most legal-name tokens appear, but exact identity is incomplete")
    elif ratio >= 0.5 and len(overlap) >= 2:
        score = 0.65
        reasons.append("partial legal-name overlap only")
    else:
        score = 0.3
        reasons.append("registry-linked URL lacks strong exact-entity identity evidence")

    status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {
        "status": status,
        "score": score,
        "publishable": status == "exact",
        "legal_name_tokens": core,
        "matched_tokens": overlap,
        "reasons": reasons,
        "method": "deterministic_name_org_evidence_v4",
    }


def assess_social_identity(profile: dict[str, Any], link: dict[str, Any]) -> dict[str, Any]:
    """Only publish an account with independent profile evidence and reciprocal domain proof.

    A website footer link or a matching-looking handle is discovery evidence only; neither
    proves that the social account belongs to the legal entity.
    """
    core = _tokens(profile.get("name"))
    link_url = str(link.get("url") or "")
    link_key = _social_profile_key(link_url)
    evidence = link.get("verification_evidence")
    if not isinstance(evidence, dict):
        evidence = {}

    website = profile.get("evidence", {}).get("website", {}) or {}
    website_value = website.get("value") or {}
    official_site_url = website_value.get("final_url") or website.get("source_url") or profile.get("website") or ""
    profile_name = str(evidence.get("profile_name") or evidence.get("display_name") or "")
    profile_tokens = set(_tokens(profile_name))
    name_matches = bool(core and set(core).issubset(profile_tokens))
    evidence_url = str(evidence.get("source_url") or "")
    evidence_key = _social_profile_key(evidence_url)
    profile_site_url = str(evidence.get("official_website_url") or evidence.get("website_url") or "")
    digest = str(evidence.get("content_sha256") or "").casefold()
    retrieved_at = str(evidence.get("retrieved_at") or "").strip()
    retrieved_at_valid = False
    if retrieved_at:
        try:
            datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
            retrieved_at_valid = True
        except ValueError:
            retrieved_at_valid = False
    method = str(evidence.get("verification_method") or "").strip()

    reasons: list[str] = []
    checks = {
        "valid_social_profile_url": link_key is not None,
        "profile_evidence_present": bool(evidence),
        "profile_url_matches_discovered_url": link_key is not None and evidence_key == link_key,
        "profile_name_matches_legal_entity": name_matches,
        "profile_links_back_to_official_site": _same_official_domain(str(official_site_url), profile_site_url),
        "profile_evidence_has_sha256": bool(re.fullmatch(r"[0-9a-f]{64}", digest)),
        "profile_evidence_has_retrieval_time": retrieved_at_valid,
        "profile_evidence_has_permitted_method": method in ALLOWED_VERIFICATION_METHODS,
    }
    reason_map = {
        "valid_social_profile_url": "social URL is not a recognized company-profile URL",
        "profile_evidence_present": "no independent profile evidence was supplied",
        "profile_url_matches_discovered_url": "captured profile evidence does not refer to the discovered account URL",
        "profile_name_matches_legal_entity": "profile display name does not match the full legal-name core",
        "profile_links_back_to_official_site": "profile does not reciprocally point to the official company's registered website domain",
        "profile_evidence_has_sha256": "profile evidence is missing a valid SHA-256 content hash",
        "profile_evidence_has_retrieval_time": "profile evidence is missing a retrieval timestamp",
        "profile_evidence_has_permitted_method": "profile evidence does not declare an approved acquisition method",
    }
    for key, passed in checks.items():
        if not passed:
            reasons.append(reason_map[key])

    ownership_verified = all(checks.values())
    return {
        "platform": link.get("platform") or (link_key[0] if link_key else "unknown"),
        "url": link_url,
        "identity_score": 0.98 if ownership_verified else 0.3,
        "publishable": ownership_verified,
        "ownership_verified": ownership_verified,
        "verification_checks": checks,
        "matched_tokens": sorted(set(core) & profile_tokens),
        "reasons": ["independent profile identity and reciprocal official-domain checks passed"] if ownership_verified else reasons,
        "method": "independent_profile_name_and_reciprocal_domain_v2",
    }


def apply_website_identity_gate(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "quarantined_social_links": 0}

    temporary_profile = {
        **profile,
        "evidence": {**profile.get("evidence", {}), "website": website},
    }
    value = website.get("value") or {}
    assessment = assess_website_identity(temporary_profile)
    value["identity_assessment"] = assessment
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original

    social_assessments = [assess_social_identity(temporary_profile, link) for link in original]
    value["social_link_assessments"] = social_assessments
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if assessment["publishable"] and item["publishable"]
    ]
    website["value"] = value
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }
