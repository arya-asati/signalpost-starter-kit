# Signalpost reference agent

This is a runnable starting point for the Signalpost company-research challenge. It is intentionally a solid baseline, not a winning submission.

The public universe contains 411,160 eligible companies. Run the starter on 100 companies before submitting. Larger local tests, including 1,000 or more companies, are encouraged but their precomputed profiles are not submitted or scored.

## What it already does

- reads a batch of Norwegian organisation numbers;
- anchors identity in the Brønnøysund bulk registry;
- fetches official financials, roles, group links and registered workplaces;
- visits the registry-listed website and rejects weak entity matches;
- emits one terminal JSONL envelope per input;
- records sources, retrieval times, content hashes, request counts and latency;
- supports checkpoint/resume and a deterministic refresh replay;
- includes examples for external-footprint discovery and an evidence-bounded research agent.

## First run: try one saved example

Requires Python 3.12+. Open a terminal inside this extracted folder.

Before downloading company data or running a full crawl, try the bundled public
sample. It uses saved responses: no API key, registry download or live web requests.

```bash
python3 scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

Open `out/refresh-demo.json`. The `events` list shows what changed between two
versions of one company profile and the source evidence for each change. The sample
should find two expected changes, no false changes, and no extra changes when the
same data is checked again.

The report's `qualification_passed` field refers only to this public sample check.
It does not qualify an entry for the competition or prove live information coverage.
The printed request counts are reads from saved responses, not network calls.

## Next: research live companies

Requires Python 3.12+ and `uv`. This step downloads data and makes live requests.
The manifest selector can create a local test batch of any size. Use 100 rows for the recommended smoke test before trying a larger batch.

```bash
uv sync --frozen
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz

uv run --frozen python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz \
  --count 100 \
  --output entry-companies.jsonl

# Use the 100-company batch as your smoke test.
cp entry-companies.jsonl smoke-companies.jsonl

uv run --frozen python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 100

# You may test at larger scale locally, but Builderr supplies the official batch for scoring.
uv run --frozen python scripts/run_competition_batch.py \
  --organisations entry-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id local-001 \
  --expected-count 1000

uv run --with pytest pytest -q
```

The published archive was clean-room verified on August 24, 2026: 104 tests and 5 subtests passed, followed by a one-company live BRREG smoke run with one terminal envelope, five requests and zero silent drops.

Increase `--count` and `--expected-count` together for a larger local test. The 100-row smoke test above is practice only; Builderr supplies the companies for every official run.

## The improvement loop

1. Treat the organisation number as the anchor.
2. Generate site/profile candidates from official data, the company site, lawful search providers and named people.
3. Save every candidate and the evidence for or against it.
4. Publish only exact-entity matches. Parent, brand, franchise and similarly named companies are not exact.
5. Crawl static HTML first. Escalate to a browser only when a deterministic completeness check fails.
6. Measure added supported coverage, wrong-company claims, runtime, requests and cost.
7. Promote a strategy only when it improves coverage without weakening the accuracy gates.
8. Freeze strategies and thresholds before the daily evaluation run.

The strongest differentiator is external evidence that remains exact and auditable: official company pages, company-owned profiles, jobs, dated activity, ratings/reviews and permitted public signals. Do not trade accuracy for volume.

## Important source rule

Open-source code does not grant permission to scrape a platform. Follow each source's terms, robots policy, rate limits and licence. LinkedIn, Meta and Indeed are useful identity/discovery targets, but direct automated collection may be restricted. Use permitted APIs, licensed providers, company-owned outbound links, or return `blocked`/`not_available`.

Read `docs/competition-control-loop.md`, `docs/external-connectors.md` and the public source policy before adding connectors.

## Submission contract

Submit a repository with:

- a 100-company smoke-test result or report;
- one documented command that accepts a JSONL batch of organisation numbers;
- exactly one terminal envelope per input;
- pinned dependencies and reproducible setup;
- a previous-snapshot input and material-change output;
- a machine-readable run report with runtime, request count and third-party cost;
- declared models, APIs, licences and source-rights assumptions.

Email the repository URL, run command, models/APIs and expected cost per 100-company run to `submit@builderr.ai`.

### Runtime batch command

The batch runner derives its expected count from the supplied input file. It does
not default official runs to 100 companies. Use the same command for the
evaluator-provided JSONL and the frozen Brønnøysund snapshot:

```bash
uv sync --frozen
uv run --frozen python scripts/run_competition_batch.py \
  --organisations "$BUILDER_BATCH_JSONL" \
  --bulk "$BRREG_SNAPSHOT_CSV" \
  --previous-snapshot "$SIGNALPOST_PREVIOUS_PROFILES" \
  --profiles-output "$SIGNALPOST_PROFILES_OUTPUT" \
  --output "$SIGNALPOST_ENVELOPES_OUTPUT" \
  --report "$SIGNALPOST_RUN_REPORT" \
  --run-id "$SIGNALPOST_RUN_ID" \
  --official-mode
```

`--official-mode` requires three evaluator values. This repository's interface
uses `SIGNALPOST_MAX_REQUESTS`, `SIGNALPOST_MAX_BYTES`, and
`SIGNALPOST_MAX_RUNTIME_SECONDS`; the runner also accepts `--max-requests`,
`--max-bytes`, and `--max-runtime-seconds`. A CLI value takes precedence over
the corresponding environment value. The contract copy does not state numeric
ceilings, and this project does not invent them or claim Builderr sets these
environment variables automatically. Configure them with the values delivered
for the run. Without `--official-mode`, unspecified limits remain unbounded.

The optional `--previous-snapshot` input is a prior internal profile JSONL for
the same organisation-number membership. The runner compares evidence by exact
organisation number, archives superseded evidence under `historical_evidence`,
and keeps historical records out of current claims. Identical evidence content
does not create repeated material-change events. A first run without a previous
snapshot is marked `initialized`.

A valid company missing from Builderr's frozen registry snapshot receives a
terminal failed result; the runner makes no live registry fallback request.
Malformed identifiers and duplicates fail preflight because they cannot form a
valid one-company-per-input identity batch.

### Budget environment interface

The names above are Signalpost's documented interface, not a claim about
Builderr's delivery mechanism. The runner resolves values in this order:

1. Explicit CLI flag.
2. Corresponding `SIGNALPOST_MAX_*` environment variable.
3. Unbounded only outside `--official-mode`.

Official mode fails before reading inputs if any of the three limits is absent.
It records which values came from flags or the environment. Runtime accounting
starts before input preparation, stops optional enrichment at exhaustion, and
continues serializing terminal results for every valid input; the report marks
whether the complete run finished within the supplied runtime.

### Contract checks

The three exact npm commands in `package.json` have these scopes:

- `npm run check:signalpost`: Python compilation and the complete pytest suite.
- `npm run check:signalpost-showcase`: validates the current offline 100-company
  smoke report and all 100 envelopes.
- `npm run check:signalpost-challenge`: runs output-contract, refresh and batch
  reliability tests, including the 1,100-company offline batch.

For a clean local test environment, run `uv sync --frozen`. The pytest
development dependency is declared in `pyproject.toml` and locked in
`uv.lock`, so no separate test-runner installation is needed. npm is used
only as a command runner; no JavaScript packages are required.

### Operational declarations

- The default official batch uses the Builderr-supplied frozen Brønnøysund
  snapshot and free Brønnøysund APIs. It has no paid API, model, or secret
  dependency. Direct third-party API cost for this default path is NOK 0; local
  compute and network infrastructure costs are outside that figure.
- The public-universe archive is a permitted local-test cache. Its SHA-256 is
  `1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384` (the
  uncompressed universe hash is
  `b82d6a3e7231d1759a958c282bc4366b80ec2fab8095053d8ed7fa9cd01bc838`). The
  official registry snapshot is supplied per run; its content hash is recorded
  in the run report.
- The offline smoke replay reads `out/smoke-profiles-final.jsonl`, a saved local
  profile cache. Optional annual-report, Fagfolkguiden, LinkedIn and other
  connector caches are external source material. They are not consumed by the
  default official batch. Any run that uses them must declare their source,
  retrieval cutoff, content hashes, retention and rights assumptions.
- Brønnøysund open data is used under NLOD 2.0. Company-site values are treated
  as first-party factual claims, not independent evidence or a grant to
  reproduce full page text. Optional NAV, Brave, LinkedIn, review and social
  connectors have separate terms and are not part of the default official
  modules. NAV's private token is read from `NAV_JOB_FEED_TOKEN`; Brave's
  optional key is read from `BRAVE_SEARCH_API_KEY` (overridable by
  `--api-key-env`). Secrets belong in environment variables, never in command
  arguments, reports or committed files.
- NAV and Brave are optional integrations. NAV's private feed is free subject
  to accepted terms. Brave may incur provider charges; no price is assumed here.
  Experimental or rights-review outputs remain quarantined. Model experiments
  described in other connector documentation are not used in the official batch.

The exact named file `docs/Signalpost_Builderr_Evaluation_Contract_v2.txt` was
not present in this checkout during the Phase 4A implementation. The available
full text copy `docs/signalpost-starter-kit.tar.txt` was preserved; no canonical
copy was created because there was no separate v2 file available for comparison.

Malformed organisation numbers and duplicate identifiers fail preflight before
enrichment. The repository's local contract requires unique organisation
numbers but does not specify how duplicates should be represented, so they are
rejected rather than emitted as duplicate envelopes.

## Evidence provenance and confidence

### Confidence semantics

The `confidence` field is a deterministic provenance heuristic. It is
not a statistically calibrated probability and has not been calibrated
against an independently labelled evaluation dataset.

The heuristic considers the evidence source class, claim availability,
and whether external evidence includes a source excerpt. Ambiguous
claims receive lower confidence; unavailable or null-valued claims
receive zero. External claims without a usable evidence span are capped
at a lower value.

The implementation and source-class mapping are maintained in
`src/norway_company_agent/output_contract.py`. These values should not
be interpreted as measured probabilities that a claim is correct.

### Source evidence and limitations

- Registry evidence retains the organisation-number row key, the
  registry snapshot hash, and the captured CSV header and data row.
- Fetched JSON responses retain their raw response hash, retrieval time,
  and a bounded excerpt of the response text.
- Website evidence includes source-derived page text when extraction
  succeeds. Blocked, failed, or unavailable requests do not receive
  fabricated excerpts.
- Accounting-obligation classifications are local ruleset
  interpretations. `ruleset_sha256` identifies the local ruleset
  identifier; it is not a hash of the linked guidance webpage. The
  guidance page is not claimed as a fetched source in these runs.
- Structural validation confirms that claim references resolve to
  evidence records. It does not independently prove that every excerpt
  semantically supports every claim.

### Local smoke-test reports

The older live-source run report `smoke-report.json` records 100 envelopes
completed on 17 October 2026. It predates the current Phase 4A code and must not
be presented as validation of this revision.

The current Phase 4A report is
`out/smoke-report-offline-phase4a-20261102.json`. It is an offline replay of
saved profiles, makes zero live research requests, records hashes for its saved
profile and registry inputs, and fingerprints the source working tree. It
validates batch/envelope/refresh behavior; it is not evidence of fresh company
research or an official competition score.
