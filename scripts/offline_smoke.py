#!/usr/bin/env python3
"""Build or validate an offline 100-company replay without network access."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.batch import read_organisation_inputs  # noqa: E402
from norway_company_agent.output_contract import validate_contract_envelopes  # noqa: E402
from scripts import run_competition_batch  # noqa: E402


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _working_tree_fingerprint() -> str:
    digest = hashlib.sha256()
    paths = [ROOT / "pyproject.toml", ROOT / "uv.lock", ROOT / "README.md", ROOT / "package.json"]
    for folder in (ROOT / "src", ROOT / "scripts", ROOT / "tests"):
        paths.extend(path for path in folder.rglob("*.py") if path.is_file())
    for path in sorted(set(paths)):
        if not path.exists():
            continue
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def validate_report(report_path: Path, envelopes_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    envelopes = _read_jsonl(envelopes_path)
    validation = validate_contract_envelopes(envelopes, expected_count=100)
    checks = {
        "offline_mode_declared": report.get("execution_mode") == "offline_saved_local_profile_replay",
        "no_live_research_requests": report.get("live_research_requests") == 0,
        "report_expected_100": report.get("expected_count") == 100,
        "report_emitted_100": report.get("emitted_envelopes") == 100,
        "report_structural_validation_passed": report.get("validation", {}).get("passed") is True,
        "actual_envelope_count_100": len(envelopes) == 100,
        "actual_envelopes_valid": validation["passed"],
        "code_fingerprint_present": bool(report.get("working_tree_fingerprint_sha256")),
    }
    result = {"passed": all(checks.values()), "checks": checks, "contract_validation": validation}
    if not result["passed"]:
        raise SystemExit("Offline smoke report validation failed: " + json.dumps(result, ensure_ascii=False))
    return result


def run_smoke(args: argparse.Namespace) -> None:
    organisation_path = Path(args.organisations)
    bulk_path = Path(args.bulk)
    saved_profiles_path = Path(args.saved_profiles)
    output_profiles = Path(args.profiles_output)
    output_envelopes = Path(args.envelopes)
    output_report = Path(args.report)

    for path in (organisation_path, bulk_path, saved_profiles_path):
        if not path.is_file():
            raise SystemExit(f"Required saved offline input is missing: {path}")
    for path in (output_profiles, output_envelopes, output_report):
        if path.exists():
            raise SystemExit(f"Refusing to overwrite existing smoke output: {path}")

    cached_profiles = _read_jsonl(saved_profiles_path)
    cached_by_org = {str(row.get("organisation_number") or ""): row for row in cached_profiles}
    if len(cached_by_org) != len(cached_profiles):
        raise SystemExit("Saved smoke profiles contain duplicate organisation numbers")
    input_orgs = [
        item["organisation_number"]
        for item in read_organisation_inputs(organisation_path)
    ]
    if len(input_orgs) != 100 or set(input_orgs) != set(cached_by_org):
        raise SystemExit("Offline smoke inputs and saved profiles must contain the same 100 companies")
    cached_websites = {
        (row.get("website") or (row.get("evidence", {}).get("website", {}).get("value") or {}).get("requested_url")): (row.get("evidence") or {}).get("website")
        for row in cached_profiles
    }

    original_fetch = run_competition_batch.fetch_official_modules
    original_website = run_competition_batch.fetch_website

    def replay_official(organisation_number: str, modules: set[str]):
        saved = cached_by_org.get(organisation_number) or {}
        saved_evidence = saved.get("evidence") or {}
        replayed = {
            module: saved_evidence[module]
            for module in modules
            if module in saved_evidence
        }
        return replayed, []

    def replay_website(url: str | None):
        record = cached_websites.get(url)
        if record is None:
            record = {
                "field": "website",
                "status": "not_fetched",
                "source_type": "agent_execution",
                "source_class": "agent_execution",
                "source_url": "",
                "retrieved_at": utc_now(),
                "note": "Offline replay had no saved website observation.",
            }
        return record, {"requests": 0, "bytes": 0, "latencies_ms": []}

    prior_argv = sys.argv
    sys.argv = [
        "run_competition_batch.py",
        "--organisations", str(organisation_path),
        "--bulk", str(bulk_path),
        "--previous-snapshot", str(saved_profiles_path),
        "--profiles-output", str(output_profiles),
        "--output", str(output_envelopes),
        "--report", str(output_report),
        "--run-id", args.run_id,
        "--workers", "8",
        "--checkpoint-every", "100",
    ]
    try:
        with (
            patch.object(run_competition_batch, "fetch_official_modules", replay_official),
            patch.object(run_competition_batch, "fetch_website", replay_website),
        ):
            try:
                run_competition_batch.main()
            except SystemExit as exc:
                if exc.code != 0:
                    raise
    finally:
        sys.argv = prior_argv
        run_competition_batch.fetch_official_modules = original_fetch
        run_competition_batch.fetch_website = original_website

    report = json.loads(output_report.read_text(encoding="utf-8"))
    report.update({
        "execution_mode": "offline_saved_local_profile_replay",
        "live_research_requests": 0,
        "saved_profile_cache": str(saved_profiles_path),
        "saved_profile_cache_sha256": _file_sha256(saved_profiles_path),
        "frozen_registry_snapshot_sha256": _file_sha256(bulk_path),
        "working_tree_fingerprint_sha256": _working_tree_fingerprint(),
        "git_commit": _git_revision(),
        "evidence_freshness_note": "Evidence is replayed from the saved local profile cache; no current external research was performed.",
    })
    output_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = validate_report(output_report, output_envelopes)
    report["smoke_validation"] = result
    output_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "run_id": report["run_id"],
        "execution_mode": report["execution_mode"],
        "expected_count": report["expected_count"],
        "emitted_envelopes": report["emitted_envelopes"],
        "live_research_requests": 0,
        "smoke_validation_passed": result["passed"],
        "working_tree_fingerprint_sha256": report["working_tree_fingerprint_sha256"],
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report", default="out/smoke-report-offline-phase4a-20261102.json")
    parser.add_argument("--envelopes", default="out/smoke-envelopes-offline-phase4a-20261102.jsonl")
    parser.add_argument("--profiles-output", default="out/smoke-profiles-offline-phase4a-20261102.jsonl")
    parser.add_argument("--organisations", default="smoke-companies.jsonl")
    parser.add_argument("--bulk", default="brreg-enheter.csv")
    parser.add_argument("--saved-profiles", default="out/smoke-profiles-final.jsonl")
    parser.add_argument("--run-id", default="offline-smoke-phase4a-20261102")
    args = parser.parse_args()
    args.report = str((ROOT / args.report).resolve())
    args.envelopes = str((ROOT / args.envelopes).resolve())
    args.profiles_output = str((ROOT / args.profiles_output).resolve())
    args.organisations = str((ROOT / args.organisations).resolve())
    args.bulk = str((ROOT / args.bulk).resolve())
    args.saved_profiles = str((ROOT / args.saved_profiles).resolve())

    if args.validate_only:
        result = validate_report(Path(args.report), Path(args.envelopes))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    run_smoke(args)


if __name__ == "__main__":
    main()
