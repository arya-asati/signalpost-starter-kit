# Free-source quality upgrade: company news, jobs, and evidence briefs

This upgrade addresses the feedback items about dated company news, job searching, short company summaries, direct sources, and conservative entity matching. It adds no paid search provider and makes no requests during installation or tests.

## 1. Website crawl coverage

The website crawler now prioritises same-site career pages (`karriere`, `jobb`, `stillinger`, `careers`, `jobs`, `vacancies`, etc.) within a bounded six-page same-site crawl (up from four pages). It also captures article publication metadata separately from modification dates (HTML article meta tags, `datePublished` JSON-LD, and `time[itemprop=datePublished]`).

A page's last-modified timestamp is not treated as its publication date. A dated company-site news observation is emitted only when:

- the registry-linked website passed the existing exact legal-entity identity gate;
- the URL is an individual article path under a news/press/aktuelt/nyheter/blog section, not just the archive root;
- a real publication date was found;
- the page has a title, main-text excerpt, retrieval timestamp, and SHA-256 hash; and
- the item is within the configured lookback window.

Company-owned news remains company-owned activity. It is never relabelled independent news, customer sentiment, reputation, or growth.

## 2. NAV job vacancy feed

NAV documents the Job Vacancy Feed as free to use. The feed requires a signed bearer JWT. NAV publishes a rotating token for experiments; a private consumer token can be requested from `nav.team.arbeidsplassen@nav.no` after confirming acceptance of NAV's terms. See:

- API docs: <https://navikt.github.io/pam-stilling-feed/>
- API specification: <https://pam-stilling-feed.ekstern.dev.nav.no/swagger>
- Terms: <https://arbeidsplassen.nav.no/vilkar-api>

The connector deliberately separates two modes:

- `--experimental-public-token`: fetches the current rotating public token at run time; every resulting observation is labelled `rights_review_experiment` / `review_required`, so it cannot enter the strict dashboard or evidence brief.
- private token via the `NAV_JOB_FEED_TOKEN` environment variable: observations use `official_api` / `approved`, subject to the consumer having accepted and followed NAV's terms.

The connector does not publish a job based on name similarity. Employer name is only a candidate pre-filter. NAV's employer `orgnr` identifies a registered Brønnøysund `underenhet` (business unit), not necessarily the parent legal entity. Signalpost therefore accepts either the exact parent number or a subunit number only when it appears in the selected parent's hashed, timestamped Brønnøysund `locations` evidence. The observation retains both numbers and the parent-child proof. A same-name but unregistered subunit is rejected. The ad must also be active and not expired, and a valid publication date must exist. Inactive events suppress earlier active versions of the same ad. Applicant/contact personal data is not retained. See NAV's [employer identifier definition](https://navikt.github.io/pam-import-api/#employer).

The optional `--diagnostic-identity-probes N` setting is disabled by default and accepts only `0` through `5`. It requires a private NAV consumer token and `--identity-probes-report PATH`. It selects only active ads whose employer/business names pass the diagnostic near-match threshold but fail the existing strict name matcher; vacancy titles are excluded. Each selected ad uses the existing NAV detail transport and counts against both the total request count and `--max-details`. Outcomes are written only to the separate diagnostic report, contain no raw response or applicant/contact data, and never become job observations or website candidates.

The CLI rejects output paths that resolve to the profiles input or to another output, including symlink aliases and existing hard links where the filesystem can identify them. Python callers retain the original three-value `run_connector` return contract; when probes are enabled, pass a mutable `identity_probe_report_out` dictionary to receive the separate diagnostic report.

When the same exact-employer record includes `employer.homepage`, the connector can also save it as a website *candidate*. That URL is not automatically trusted. `verify_nav_employer_websites.py` independently fetches it and applies Signalpost's existing exact-legal-entity website gate; parent/group sites and ambiguous pages remain quarantined. The verifier refuses public-token/experimental candidates. This creates a no-paid-search discovery path for companies that advertise on NAV, but it will not discover every company that has no registry website.

NAV's terms require consumers to remove inactive/deleted ads and reflect updates, and place data-protection responsibilities on the consumer. Refresh the feed before using job results in the product; do not treat a saved job snapshot as live status.

## 3. Evidence-linked company brief

`scripts/build_evidence_briefs.py` generates a short deterministic company summary and a `why_relevant` section. It uses only facts with direct HTTPS/HTTP source URLs, retrieval timestamps, and SHA-256 hashes. It separates official registry facts, official annual-account figures, company-reported website descriptions, dated company-site activity, and exact-employer job posts.

The `why_relevant` section is a list of traceable indicators, not an investment recommendation or a company-quality score. When no source-backed indicator is available, it explicitly reports `insufficient_evidence` rather than inventing a reason.

## 4. Pilot commands

First run tests; this has no network dependency:

```bash
uv run --with pytest pytest -q
```

After reading NAV's terms, a bounded experiment can use the rotating public token (no card and no paid API plan):

```bash
uv run python scripts/run_nav_jobs_connector.py \
  --profiles out/smoke-profiles-audit-005.jsonl \
  --output out/nav-jobs-pilot-001.jsonl \
  --report out/nav-jobs-pilot-report-001.json \
  --website-candidates-output out/nav-website-candidates-pilot-001.jsonl \
  --experimental-public-token \
  --max-pages 10 \
  --max-details 150 \
  --min-interval 0.25
```

That pilot is for coverage diagnostics only. Its job observations and homepage candidates remain quarantined by design. Diagnostic identity probes require a private token and cannot run with the rotating public token. The website verifier will refuse the experimental homepage candidates. Do not use them as published/awardable signals.

For production-eligible use, request a private NAV consumer token after accepting the terms. Configure it locally (never commit or share it):

```bash
read -s "NAV_JOB_FEED_TOKEN?Paste your NAV private token: "
echo
export NAV_JOB_FEED_TOKEN
```

Then rerun the connector without `--experimental-public-token`, add `--website-candidates-output out/nav-website-candidates-001.jsonl`, and inspect `truncated`, `detail_errors`, `companies_with_verified_jobs`, `employer_homepage_candidates`, and `requests_total_including_token` before relying on coverage.

Independently verify the new homepage candidates into a separate profile file (the original 100-company profile file is not overwritten):

```bash
uv run python scripts/verify_nav_employer_websites.py \
  --profiles out/smoke-profiles-audit-005.jsonl \
  --candidates out/nav-website-candidates-001.jsonl \
  --output out/profiles-nav-site-verified-001.jsonl \
  --report out/nav-website-verification-report-001.json \
  --max-candidates 100 \
  --max-per-company 2
```

Only exact-site identity-gate passes enter the canonical `website` field. Review the report's `verified_and_promoted`, `quarantined_identity_not_proven`, `skipped_experimental_or_unapproved`, and `requests` before comparing coverage.

Build the short briefs after the site-news and job observations exist:

```bash
uv run python scripts/build_evidence_briefs.py \
  --profiles out/profiles-nav-site-verified-001.jsonl \
  --external-observations out/company-site-news.jsonl out/nav-jobs-001.jsonl \
  --output out/company-evidence-briefs.jsonl
```

The experimental NAV output will not add job relevance indicators or verified homepage candidates because it is intentionally non-publishable. Use a private-token run for strict output. Only verified companies with an accepted NAV homepage candidate can gain additional website coverage; companies with no such source remain unresolved and must be reported as such.

## 5. Quality gates before promotion

Do not promote this as a competition improvement until the real local run demonstrates:

- zero wrong-employer publications (exact NAV organisation number required);
- no inactive or expired vacancies in the current displayed results;
- dated news coverage only from verified exact-company sites;
- zero undated news items published as dated activity;
- every displayed claim has a direct source, retrieval timestamp, and valid hash;
- request counts include the token, every feed page, every detail call, and retries/redirects if added;
- a comparison report shows added exact coverage, abstentions, and errors on the same frozen 100-company batch; and
- the full test suite passes.

These changes target the feedback categories; they do not establish an official score. The organiser's hidden scoring and holdout data remain authoritative.
