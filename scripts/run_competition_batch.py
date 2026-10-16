
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import (  # noqa: E402
    profile_complete_for_modules,
    profiles_from_bulk,
    read_organisation_inputs,
    terminal_envelope,
    validate_envelopes,
)
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.output_contract import (  # noqa: E402
    contract_envelope,
    validate_contract_envelopes,
)
from norway_company_agent.website import fetch_website  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )

    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Signalpost company research batch runner"
    )
    parser.add_argument(
        "--organisations",
        required=True,
        help="JSON, JSONL, or text organisation-number list",
    )
    parser.add_argument(
        "--bulk",
        required=True,
        help="Frozen Brreg entity snapshot",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Contract-shaped terminal envelope JSONL",
    )
    parser.add_argument(
        "--profiles-output",
        required=True,
        help="Internal company profile JSONL",
    )
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--modules",
        default=(
            "registry,accounting_obligation,registry_live,"
            "financials,roles,group,locations,website"
        ),
    )
    args = parser.parse_args()

    if args.expected_count < 1:
        raise SystemExit("--expected-count must be positive")

    if args.workers < 1:
        raise SystemExit("--workers must be positive")

    if args.checkpoint_every < 1:
        raise SystemExit("--checkpoint-every must be positive")

    started_at = utc_now()

    organisation_inputs = read_organisation_inputs(
        args.organisations
    )
    orgs = [
        item["organisation_number"]
        for item in organisation_inputs
    ]

    if len(orgs) != args.expected_count:
        raise SystemExit(
            f"Expected {args.expected_count} organisations, "
            f"received {len(orgs)}"
        )

    profiles, registry_metadata = profiles_from_bulk(
        args.bulk,
        orgs,
    )

    annotations = {
        item["organisation_number"]: item
        for item in organisation_inputs
    }

    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[
                    profile["organisation_number"]
                ][key]

    requested_modules = [
        item.strip()
        for item in args.modules.split(",")
        if item.strip()
    ]

    if len(requested_modules) != len(set(requested_modules)):
        raise SystemExit("Duplicate modules are not allowed")

    fetch_modules = set(requested_modules) - {
        "registry",
        "accounting_obligation",
        "website",
    }

    operations = {
        "requests": 0,
        "bytes": 0,
        "latencies_ms": [],
    }

    def enrich(profile: dict) -> tuple[dict, dict]:
        profile_started = time.perf_counter()

        records, metrics = fetch_official_modules(
            profile["organisation_number"],
            fetch_modules,
        )
        profile["evidence"].update(records)

        website_metrics = {
            "requests": 0,
            "bytes": 0,
            "latencies_ms": [],
        }

        if "website" in requested_modules:
            website_record, website_metrics = fetch_website(
                profile.get("website")
            )

            gated = apply_website_identity_gate(
                profile,
                website_record,
            )
            profile["evidence"]["website"] = gated["website"]

        metric = {
            "requests": (
                len(metrics) + website_metrics["requests"]
            ),
            "bytes": (
                sum(item.bytes_received for item in metrics)
                + website_metrics["bytes"]
            ),
            "latencies_ms": (
                [item.elapsed_ms for item in metrics]
                + website_metrics["latencies_ms"]
            ),
            "runtime_ms": max(
    1,
    round((time.perf_counter() - profile_started) * 1000),
),
        }

        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)

    if args.resume and profiles_output.exists():
        prior = [
            json.loads(line)
            for line in profiles_output.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]

        if not set(
            item["organisation_number"] for item in prior
        ).issubset(set(orgs)):
            raise SystemExit(
                "Resume profile membership is not a subset "
                "of this batch"
            )

        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(
                item,
                requested_modules,
            )
        }
        resumed_profiles = len(state)

    pending_profiles = [
        profile
        for profile in profiles
        if profile["organisation_number"] not in state
    ]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(enrich, profile): profile["organisation_number"]
            for profile in pending_profiles
        }

        for index, future in enumerate(
            as_completed(futures),
            1,
        ):
            profile, metric = future.result()

            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(
                metric["latencies_ms"]
            )

            if (
                index % args.checkpoint_every == 0
                or index == len(pending_profiles)
            ):
                checkpoint = [
                    state[org]
                    for org in orgs
                    if org in state
                ]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]

    # Keep the original internal envelope validation.
    internal_envelopes = [
        terminal_envelope(
            profile,
            run_id=args.run_id,
            modules=requested_modules,
            started_at=started_at,
            completed_at=completed_at,
        )
        for profile in ordered_profiles
    ]

    internal_validation = validate_envelopes(
        internal_envelopes,
        args.expected_count,
    )

    # Build the contract-shaped output without replacing profiles.
    contract_envelopes = [
        contract_envelope(
            profile,
            run_id=args.run_id,
            modules=requested_modules,
            started_at=started_at,
            completed_at=completed_at,
        )
        for profile in ordered_profiles
    ]

    contract_validation = validate_contract_envelopes(
        contract_envelopes,
        args.expected_count,
    )

    validation = {
        "passed": (
            internal_validation["passed"]
            and contract_validation["passed"]
        ),
        "checks": {
            **{
                f"internal_{name}": passed
                for name, passed in (
                    internal_validation["checks"].items()
                )
            },
            **{
                f"contract_{name}": passed
                for name, passed in (
                    contract_validation["checks"].items()
                )
            },
        },
        "internal_validation": internal_validation,
        "contract_validation": contract_validation,
    }

    # Internal profiles remain available for the local scorer.
    write_jsonl(profiles_output, ordered_profiles)

    # The primary output now contains contract-shaped envelopes.
    write_jsonl(
        Path(args.output),
        contract_envelopes,
    )

    latencies = sorted(
        operations.pop("latencies_ms")
    )

    operations["p50_ms"] = (
        latencies[len(latencies) // 2]
        if latencies
        else None
    )
    operations["p95_ms"] = (
        latencies[
            min(
                len(latencies) - 1,
                int(len(latencies) * 0.95),
            )
        ]
        if latencies
        else None
    )

    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(contract_envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "validation": validation,
    }

    report_path = Path(args.report)
    report_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    report_path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )
    )

    raise SystemExit(
        0 if validation["passed"] else 1
    )


if __name__ == "__main__":
    main()
