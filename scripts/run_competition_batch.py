
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
from norway_company_agent.runtime_budget import (  # noqa: E402
    BudgetExceeded,
    RunBudget,
    use_run_budget,
)
from norway_company_agent.refresh import refresh_profile  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402


def write_jsonl(path: Path, rows: list[dict], *, budget: RunBudget | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            if budget is not None:
                budget.observe_time()
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )

    temporary.replace(path)


def _resolved_budget_value(
    cli_value: int | float | None,
    env_name: str,
    *,
    value_type: type,
    parser: argparse.ArgumentParser,
) -> tuple[int | float | None, str]:
    """CLI values override the documented environment-variable interface."""
    raw_env = os.environ.get(env_name)
    if cli_value is not None:
        value = cli_value
        source = "cli"
    elif raw_env is not None and raw_env.strip():
        try:
            value = value_type(raw_env.strip())
        except ValueError:
            parser.error(f"{env_name} must be a valid {value_type.__name__}")
        source = "environment"
    else:
        return None, "unset"

    if isinstance(value, float) and not math.isfinite(value):
        parser.error(f"{env_name} must be finite")
    if value < 0:
        parser.error(f"{env_name} cannot be negative")
    return value, source


def _ensure_distinct_paths(inputs: list[Path], outputs: list[Path]) -> None:
    """Reject output aliases before any input or output file is written."""
    resolved_inputs = [path.resolve(strict=False) for path in inputs]
    write_targets = outputs + [
        path.with_suffix(path.suffix + ".tmp")
        for path in outputs
    ]
    resolved_outputs = [path.resolve(strict=False) for path in write_targets]
    for index, output in enumerate(write_targets):
        output_resolved = resolved_outputs[index]
        for input_path, input_resolved in zip(inputs, resolved_inputs):
            same = output_resolved == input_resolved
            if not same and output.exists() and input_path.exists():
                try:
                    same = os.path.samefile(output, input_path)
                except OSError:
                    same = False
            if same:
                raise SystemExit(f"Output path collides with input: {output}")
        for other_index, other in enumerate(write_targets):
            if index == other_index:
                continue
            other_resolved = resolved_outputs[other_index]
            same = output_resolved == other_resolved
            if not same and output.exists() and other.exists():
                try:
                    same = os.path.samefile(output, other)
                except OSError:
                    same = False
            if same:
                raise SystemExit(f"Output paths collide: {output} and {other}")


def _load_previous_snapshot(path: Path, organisation_numbers: list[str]) -> dict[str, dict]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    prior_orgs = [str(row.get("organisation_number") or "") for row in rows]
    if len(prior_orgs) != len(set(prior_orgs)):
        raise SystemExit("Previous snapshot contains duplicate organisation numbers")
    if set(prior_orgs) != set(organisation_numbers):
        raise SystemExit("Previous snapshot membership does not match the current batch")
    return {org: row for org, row in zip(prior_orgs, rows)}

def record_worker_failure(
    profile: dict,
    requested_modules: list[str],
    error: Exception,
    completed_at: str,
) -> dict:
    """Preserve a company result when an unexpected worker error occurs."""

    evidence_records = profile.setdefault("evidence", {})
    detail = f"{type(error).__name__}: {str(error).strip()[:240]}"

    if not str(error).strip():
        detail = type(error).__name__

    missing_modules = []

    for module in requested_modules:
        if module in evidence_records:
            continue

        evidence_records[module] = {
            "field": module,
            "status": "source_error",
            "source_type": "agent_execution",
            "source_class": "agent_execution",
            "retrieved_at": completed_at,
            "note": (
                "Worker failed before evidence was produced. "
                f"Error: {detail}"
            ),
        }
        missing_modules.append(module)

    profile.setdefault("errors", []).append({
        "kind": "worker_exception",
        "message": detail,
        "modules_missing_evidence": missing_modules,
    })

    # Unknown metrics remain unknown rather than being reported as zero.
    profile["run_metrics"] = {
        "requests": None,
        "bytes": None,
        "latencies_ms": [],
        "runtime_ms": None,
        "third_party_cost_usd": None,
        "metrics_complete": False,
    }

    return profile


def record_budget_exhaustion(
    profile: dict,
    requested_modules: list[str],
    dimension: str,
    completed_at: str,
) -> dict:
    """Mark unchecked modules failed when a run-level cap stops enrichment."""
    evidence_records = profile.setdefault("evidence", {})
    for module in requested_modules:
        if module in evidence_records:
            continue
        evidence_records[module] = {
            "field": module,
            "status": "budget_exhausted",
            "source_type": "agent_execution",
            "source_class": "agent_execution",
            "retrieved_at": completed_at,
            "note": f"Optional enrichment stopped by the {dimension} run budget.",
        }
    profile.setdefault("errors", []).append({
        "kind": "budget_exhausted",
        "dimension": dimension,
        "message": "Optional enrichment stopped; terminal company output is retained.",
    })
    return profile


def bounded_futures(pool, function, items, max_pending: int):
    """Yield completed futures while keeping submitted work bounded."""
    iterator = iter(items)
    pending = {}
    exhausted = False

    while pending or not exhausted:
        while not exhausted and len(pending) < max_pending:
            try:
                item = next(iterator)
            except StopIteration:
                exhausted = True
                break
            pending[pool.submit(function, item)] = item

        if not pending:
            break

        completed, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in completed:
            yield future, pending.pop(future)

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
    parser.add_argument(
        "--previous-snapshot",
        help="Prior internal profile JSONL for refresh comparison",
    )
    parser.add_argument(
        "--official-mode",
        action="store_true",
        help="Require all evaluator budget values from CLI flags or environment",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        help="Optional expected input count; defaults to the supplied batch size",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--max-runtime-seconds", type=float)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--modules",
        default=(
            "registry,accounting_obligation,registry_live,"
            "financials,roles,group,locations,website"
        ),
    )
    args = parser.parse_args()

    budget_values = {}
    budget_sources = {}
    for name, env_name, value_type in (
        ("max_requests", "SIGNALPOST_MAX_REQUESTS", int),
        ("max_bytes", "SIGNALPOST_MAX_BYTES", int),
        ("max_runtime_seconds", "SIGNALPOST_MAX_RUNTIME_SECONDS", float),
    ):
        value, source = _resolved_budget_value(
            getattr(args, name),
            env_name,
            value_type=value_type,
            parser=parser,
        )
        budget_values[name] = value
        budget_sources[name] = source

    if args.official_mode:
        missing_limits = [name for name, value in budget_values.items() if value is None]
        if missing_limits:
            parser.error(
                "--official-mode requires evaluator values for: "
                + ", ".join(missing_limits)
                + ". Set the documented SIGNALPOST_MAX_* variables or pass CLI flags."
            )

    if args.expected_count is not None and args.expected_count < 1:
        raise SystemExit("--expected-count must be positive")

    if args.workers < 1:
        raise SystemExit("--workers must be positive")

    if args.checkpoint_every < 1:
        raise SystemExit("--checkpoint-every must be positive")

    input_paths = [Path(args.organisations), Path(args.bulk)]
    if args.previous_snapshot:
        input_paths.append(Path(args.previous_snapshot))
    output_paths = [Path(args.profiles_output), Path(args.output), Path(args.report)]
    _ensure_distinct_paths(input_paths, output_paths)

    started_at = utc_now()
    budget = RunBudget(
        max_requests=budget_values["max_requests"],
        max_bytes=budget_values["max_bytes"],
        max_runtime_seconds=budget_values["max_runtime_seconds"],
    )

    organisation_inputs = read_organisation_inputs(
        args.organisations
    )
    budget.observe_time()
    orgs = [
        item["organisation_number"]
        for item in organisation_inputs
    ]
    expected_count = (
        len(orgs)
        if args.expected_count is None
        else args.expected_count
    )

    if not orgs:
        raise SystemExit("The organisation batch must not be empty")

    if len(orgs) != expected_count:
        raise SystemExit(
            f"Expected {expected_count} organisations, "
            f"received {len(orgs)}"
        )

    profiles, registry_metadata = profiles_from_bulk(
        args.bulk,
        orgs,
    )
    budget.observe_time()

    previous_profiles = (
        _load_previous_snapshot(Path(args.previous_snapshot), orgs)
        if args.previous_snapshot
        else {}
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
    "worker_failures": 0,
    "request_metrics_complete": True,
    "bytes_metrics_complete": True,
    "latency_metrics_complete": True,
}

    def enrich(profile: dict) -> tuple[dict, dict]:
        profile_started = time.perf_counter()

        try:
            budget.check_time()
        except BudgetExceeded as exc:
            record_budget_exhaustion(
                profile,
                requested_modules,
                exc.dimension,
                utc_now(),
            )
            metric = {
                "requests": 0,
                "bytes": 0,
                "latencies_ms": [],
                "runtime_ms": max(
                    1,
                    round((time.perf_counter() - profile_started) * 1000),
                ),
            }
            profile["run_metrics"] = metric
            return profile, metric

        snapshot_missing = any(
            isinstance(error, dict)
            and error.get("kind") == "registry_snapshot_missing"
            for error in profile.get("errors") or []
        )

        if snapshot_missing:
            message = (
                "Enrichment was not attempted because this organisation "
                "is absent from the supplied registry snapshot."
            )
            for module in requested_modules:
                if module in profile["evidence"]:
                    continue
                profile["evidence"][module] = {
                    "field": module,
                    "status": "source_error",
                    "source_type": "agent_execution",
                    "source_class": "agent_execution",
                    "retrieved_at": utc_now(),
                    "note": message,
                }
            metric = {
                "requests": 0,
                "bytes": 0,
                "latencies_ms": [],
                "runtime_ms": max(
                    1,
                    round((time.perf_counter() - profile_started) * 1000),
                ),
            }
            profile["run_metrics"] = metric
            return profile, metric
                # Capture this worker's usage before processing this company.
        thread_usage_before = budget.current_thread_usage()

        with use_run_budget(budget):
            try:
                records, metrics = fetch_official_modules(
                    profile["organisation_number"],
                    fetch_modules,
                )
            except BudgetExceeded as exc:
                records, metrics = {}, []
                record_budget_exhaustion(
                    profile,
                    list(fetch_modules),
                    exc.dimension,
                    utc_now(),
                )
            profile["evidence"].update(records)

        website_metrics = {
            "requests": 0,
            "bytes": 0,
            "latencies_ms": [],
        }

        if "website" in requested_modules:
            if budget.exhausted:
                dimension = budget.exhausted_by or "runtime"
                record_budget_exhaustion(
                    profile,
                    ["website"],
                    dimension,
                    utc_now(),
                )
            else:
                try:
                    with use_run_budget(budget):
                        website_record, website_metrics = fetch_website(
                            profile.get("website")
                        )
                    gated = apply_website_identity_gate(
                        profile,
                        website_record,
                    )
                    profile["evidence"]["website"] = gated["website"]
                except BudgetExceeded as exc:
                    record_budget_exhaustion(
                        profile,
                        ["website"],
                        exc.dimension,
                        utc_now(),
                    )
                            # Calculate this company's usage, not the worker's cumulative usage.
        thread_usage_after = budget.current_thread_usage()

        thread_request_delta = max(
            0,
            thread_usage_after["requests"]
            - thread_usage_before["requests"],
        )

        thread_byte_delta = max(
            0,
            thread_usage_after["bytes"]
            - thread_usage_before["bytes"],
        )

        metric = {
            "requests": max(
                sum(
                    int(getattr(item, "request_count", 1) or 0)
                    for item in metrics
                )
                + website_metrics["requests"],
                thread_request_delta,
            ),
            "bytes": max(
                sum(item.bytes_received for item in metrics)
                + website_metrics["bytes"],
                thread_byte_delta,
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
        completed_profiles = bounded_futures(
            pool,
            enrich,
            pending_profiles,
            max_pending=args.workers,
        )

        for index, (future, original_profile) in enumerate(
            completed_profiles,
            1,
        ):
            try:
                profile, metric = future.result()
            except Exception as exc:
                profile = record_worker_failure(
                    original_profile,
                    requested_modules,
                    exc,
                    utc_now(),
                )
                metric = profile["run_metrics"]

                operations["worker_failures"] += 1
                operations["request_metrics_complete"] = False
                operations["bytes_metrics_complete"] = False
                operations["latency_metrics_complete"] = False

            state[profile["organisation_number"]] = profile

            if metric.get("requests") is None:
                operations["request_metrics_complete"] = False
            else:
                operations["requests"] += metric["requests"]

            if metric.get("bytes") is None:
                operations["bytes_metrics_complete"] = False
            else:
                operations["bytes"] += metric["bytes"]

            operations["latencies_ms"].extend(
                metric.get("latencies_ms") or []
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
                write_jsonl(profiles_output, checkpoint, budget=budget)
    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]

    observed_modules = set(requested_modules) | {"registry", "accounting_obligation"}
    if previous_profiles:
        ordered_profiles = [
            refresh_profile(
                previous_profiles[profile["organisation_number"]],
                profile,
                observed_modules=observed_modules,
            )
            for profile in ordered_profiles
        ]
    else:
        for profile in ordered_profiles:
            profile["refresh_checked"] = True
            profile["previous_snapshot_available"] = False
            profile["refresh_metadata"] = {
                "status": "initialized",
                "previous_snapshot_available": False,
                "idempotent": False,
                "change_count": 0,
            }

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
        expected_count,
        orgs,
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
        expected_count,
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

    if not validation["passed"]:
        raise SystemExit(
            "Final envelopes failed validation; primary output was not written"
        )

    # Internal profiles remain available for the local scorer.
    write_jsonl(profiles_output, ordered_profiles, budget=budget)

    # The primary output now contains contract-shaped envelopes.
    write_jsonl(
        Path(args.output),
        contract_envelopes,
        budget=budget,
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
    budget_report = budget.snapshot()
    operations["requests"] = max(
        operations["requests"],
        budget_report["requests"],
    )
    operations["bytes"] = max(
        operations["bytes"],
        budget_report["bytes_received"],
    )
    try:
        from norway_company_agent.operations import peak_rss_bytes

        operations["peak_rss_bytes"] = peak_rss_bytes()
    except (ImportError, AttributeError, OSError):
        operations["peak_rss_bytes"] = None

    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": utc_now(),
        "official_mode": args.official_mode,
        "budget_sources": budget_sources,
        "expected_count": expected_count,
        "emitted_envelopes": len(contract_envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "resource_budget": budget_report,
        "execution_within_runtime_budget": (
            budget_values["max_runtime_seconds"] is None
            or budget_report["elapsed_seconds"] <= budget_values["max_runtime_seconds"]
        ),
        "refresh": {
            "checked": bool(previous_profiles),
            "previous_snapshot_available": bool(previous_profiles),
            "material_change_count": sum(
                len(profile.get("changes") or [])
                for profile in ordered_profiles
            ),
            "idempotent": (
                bool(previous_profiles)
                and all(
                    (profile.get("refresh_metadata") or {}).get("idempotent") is True
                    for profile in ordered_profiles
                )
            ),
        },
        "validation": validation,
    }

    report_path = Path(args.report)
    report_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    budget.observe_time()
    report["resource_budget"] = budget.snapshot()
    report["execution_within_runtime_budget"] = (
        budget_values["max_runtime_seconds"] is None
        or report["resource_budget"]["elapsed_seconds"] <= budget_values["max_runtime_seconds"]
    )
    temporary_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    budget.observe_time()
    temporary_report.replace(report_path)

    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )
    )

    raise SystemExit(
    0
    if validation["passed"] and operations["worker_failures"] == 0
    else 1
)

if __name__ == "__main__":
    main()
