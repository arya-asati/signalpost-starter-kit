
from __future__ import annotations

import csv
import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.batch import profiles_from_bulk, read_organisation_inputs
from norway_company_agent.evidence import evidence
from norway_company_agent.http import fetch_json
from norway_company_agent.runtime_budget import (
    BudgetExceeded,
    RunBudget,
    current_run_budget,
    use_run_budget,
)
from scripts import run_competition_batch


CSV_FIELDS = [
    "organisasjonsnummer",
    "navn",
    "organisasjonsform.kode",
    "antallAnsatte",
    "konkurs",
    "underAvvikling",
    "forretningsadresse.kommune",
    "forretningsadresse.kommunenummer",
    "naeringskode1.kode",
    "naeringskode1.beskrivelse",
    "hjemmeside",
    "sisteInnsendteAarsregnskap",
]


def valid_org_number(prefix: int) -> str:
    first_eight = f"{prefix:08d}"
    weighted_sum = sum(
        int(digit) * weight
        for digit, weight in zip(first_eight, (3, 2, 7, 6, 5, 4, 3, 2))
    )
    check_digit = 11 - weighted_sum % 11
    if check_digit == 11:
        check_digit = 0
    if check_digit == 10:
        return valid_org_number(prefix + 1)
    return first_eight + str(check_digit)


def valid_org_numbers(count: int, start: int) -> list[str]:
    rows = []
    prefix = start

    while len(rows) < count:
        first_eight = f"{prefix:08d}"
        weighted_sum = sum(
            int(digit) * weight
            for digit, weight in zip(first_eight, (3, 2, 7, 6, 5, 4, 3, 2))
        )
        check_digit = 11 - weighted_sum % 11

        if check_digit == 11:
            check_digit = 0

        if check_digit != 10:
            rows.append(first_eight + str(check_digit))

        prefix += 1

    return rows


def write_batch_files(
    root: Path,
    orgs: list[str],
    snapshot_orgs: list[str] | None = None,
):
    input_path = root / "organisations.jsonl"
    input_path.write_text(
        "".join(
            json.dumps({"organisation_number": org}) + "\n"
            for org in orgs
        ),
        encoding="utf-8",
    )

    bulk_path = root / "brreg.csv.gz"
    rows = snapshot_orgs if snapshot_orgs is not None else orgs

    with bulk_path.open("wb") as raw_handle:
        import gzip

        with gzip.GzipFile(fileobj=raw_handle, mode="wb") as zipped:
            text = io.TextIOWrapper(
                zipped,
                encoding="utf-8",
                newline="",
            )
            writer = csv.DictWriter(
                text,
                fieldnames=CSV_FIELDS,
                delimiter=";",
            )
            writer.writeheader()

            for index, org in enumerate(rows):
                writer.writerow({
                    "organisasjonsnummer": org,
                    "navn": f"Synthetic Company {index} AS",
                    "organisasjonsform.kode": "AS",
                    "antallAnsatte": "4",
                    "konkurs": "false",
                    "underAvvikling": "false",
                    "forretningsadresse.kommune": "Oslo",
                    "forretningsadresse.kommunenummer": "0301",
                    "naeringskode1.kode": "62.010",
                    "naeringskode1.beskrivelse": "Computer programming",
                    "hjemmeside": "",
                    "sisteInnsendteAarsregnskap": "2025",
                })

            text.flush()
            text.detach()

    return input_path, bulk_path


def invoke_batch(
    monkeypatch,
    tmp_path,
    orgs,
    *,
    snapshot_orgs=None,
    extra_args=(),
    modules="registry",
    workers=4,
):
    input_path, bulk_path = write_batch_files(
        tmp_path,
        orgs,
        snapshot_orgs,
    )

    output_path = tmp_path / "envelopes.jsonl"
    profiles_path = tmp_path / "profiles.jsonl"
    report_path = tmp_path / "report.json"

    argv = [
        "run_competition_batch.py",
        "--organisations", str(input_path),
        "--bulk", str(bulk_path),
        "--output", str(output_path),
        "--profiles-output", str(profiles_path),
        "--report", str(report_path),
        "--run-id", "offline-reliability-test",
        "--workers", str(workers),
        "--checkpoint-every", "500",
        "--modules", modules,
        *[str(value) for value in extra_args],
    ]

    monkeypatch.setattr("sys.argv", argv)

    with pytest.raises(SystemExit) as result:
        run_competition_batch.main()

    assert result.value.code == 0

    output_rows = [
        json.loads(line)
        for line in output_path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]

    report = json.loads(
        report_path.read_text(encoding="utf-8")
    )

    return output_rows, report


@pytest.mark.parametrize("count", [100, 1000, 1100])
def test_batch_sizes_emit_one_ordered_terminal_result_per_input(
    monkeypatch,
    tmp_path,
    count,
):
    orgs = valid_org_numbers(count, 20_000_000)

    output_rows, report = invoke_batch(
        monkeypatch,
        tmp_path,
        orgs,
    )

    assert len(output_rows) == count
    assert [
        row["organisation_number"] for row in output_rows
    ] == orgs

    assert len({
        row["organisation_number"] for row in output_rows
    }) == count

    assert all(
        row["run"]["terminal_status"] == "completed"
        for row in output_rows
    )

    assert report["emitted_envelopes"] == count
    assert report["validation"]["passed"] is True


def test_missing_snapshot_company_gets_failed_result_without_fetch(
    monkeypatch,
    tmp_path,
):
    present = "923609016"
    missing = valid_org_number(30_000_001)
    calls = []

    def fake_official(org, modules):
        calls.append(org)
        return {}, []

    monkeypatch.setattr(
        run_competition_batch,
        "fetch_official_modules",
        fake_official,
    )

    rows, report = invoke_batch(
        monkeypatch,
        tmp_path,
        [present, missing],
        snapshot_orgs=[present],
        modules="registry,roles",
    )

    assert calls == [present]

    assert [
        row["organisation_number"] for row in rows
    ] == [present, missing]

    missing_claims = {
        claim["field"]: claim
        for claim in rows[1]["claims"]
    }

    assert missing_claims["registry"]["availability"] == "failed"
    assert missing_claims["roles"]["availability"] == "failed"

    assert report["registry"]["snapshot_missing"] == 1
    assert rows[1]["operations"]["requests"] == 0


def test_previous_snapshot_preserves_changed_evidence_without_republishing_history(
    monkeypatch,
    tmp_path,
):
    org = valid_org_number(35_000_001)

    input_path, bulk_path = write_batch_files(
        tmp_path,
        [org],
    )

    previous_profiles, _ = profiles_from_bulk(
        bulk_path,
        [org],
    )

    old_roles = evidence(
        "roles",
        "available",
        "official_roles",
        f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller",
        value={"roles": [{"role": "Board member"}]},
        content_sha256="b" * 64,
        retrieved_at="2026-01-01T00:00:00Z",
    )

    previous_profiles[0]["evidence"]["roles"] = old_roles

    previous_path = tmp_path / "previous.jsonl"
    previous_path.write_text(
        json.dumps(previous_profiles[0]) + "\n",
        encoding="utf-8",
    )

    def fake_official(organisation_number, modules):
        return {
            "roles": evidence(
                "roles",
                "available",
                "official_roles",
                (
                    "https://data.brreg.no/enhetsregisteret/api/"
                    f"enheter/{organisation_number}/roller"
                ),
                value={"roles": [{"role": "Chair"}]},
                content_sha256="c" * 64,
                retrieved_at="2026-02-01T00:00:00Z",
            )
        }, []

    monkeypatch.setattr(
        run_competition_batch,
        "fetch_official_modules",
        fake_official,
    )

    output_path = tmp_path / "envelopes.jsonl"
    profiles_path = tmp_path / "profiles.jsonl"
    report_path = tmp_path / "report.json"

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_competition_batch.py",
            "--organisations", str(input_path),
            "--bulk", str(bulk_path),
            "--previous-snapshot", str(previous_path),
            "--output", str(output_path),
            "--profiles-output", str(profiles_path),
            "--report", str(report_path),
            "--run-id", "refresh-offline-test",
            "--workers", "1",
            "--modules", "registry,accounting_obligation,roles",
        ],
    )

    with pytest.raises(SystemExit) as result:
        run_competition_batch.main()

    assert result.value.code == 0

    envelope = json.loads(
        output_path.read_text(
            encoding="utf-8"
        ).splitlines()[0]
    )

    current_roles_claim = next(
        claim
        for claim in envelope["claims"]
        if claim["field"] == "roles.roles[0].role"
    )

    assert current_roles_claim["value"] == "Chair"

    historical = [
        item
        for item in envelope["historical_evidence"]
        if item["module"] == "roles"
    ]

    assert len(historical) == 1

    assert (
        historical[0]["record"]["value"]["roles"][0]["role"]
        == "Board member"
    )

    assert historical[0]["temporal_status"] == "historical"

    assert all(
        historical[0]["id"] not in claim["evidence_ids"]
        for claim in envelope["claims"]
    )

    assert envelope["refresh_metadata"]["status"] == "checked"

    assert any(
        change["field"] == "evidence.roles"
        and change["status"] == "changed"
        for change in envelope["changes"]
    )


def test_cli_and_environment_budget_precedence_and_official_mode(
    monkeypatch,
    tmp_path,
):
    org = valid_org_number(36_000_001)

    monkeypatch.setenv("SIGNALPOST_MAX_REQUESTS", "9")
    monkeypatch.setenv("SIGNALPOST_MAX_BYTES", "1000")
    monkeypatch.setenv("SIGNALPOST_MAX_RUNTIME_SECONDS", "20")

    rows, report = invoke_batch(
        monkeypatch,
        tmp_path,
        [org],
        extra_args=(
            "--official-mode",
            "--max-requests",
            "7",
        ),
    )

    assert len(rows) == 1

    assert report["resource_budget"]["limits"] == {
        "requests": 7,
        "bytes": 1000,
        "runtime_seconds": 20.0,
    }

    assert report["budget_sources"] == {
        "max_requests": "cli",
        "max_bytes": "environment",
        "max_runtime_seconds": "environment",
    }


def test_official_mode_requires_all_evaluator_budgets(
    monkeypatch,
    tmp_path,
    capsys,
):
    monkeypatch.delenv(
        "SIGNALPOST_MAX_REQUESTS",
        raising=False,
    )
    monkeypatch.delenv(
        "SIGNALPOST_MAX_BYTES",
        raising=False,
    )
    monkeypatch.delenv(
        "SIGNALPOST_MAX_RUNTIME_SECONDS",
        raising=False,
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_competition_batch.py",
            "--organisations", str(tmp_path / "input.jsonl"),
            "--bulk", str(tmp_path / "registry.csv"),
            "--previous-snapshot", str(tmp_path / "previous.jsonl"),
            "--output", str(tmp_path / "out.jsonl"),
            "--profiles-output", str(tmp_path / "profiles.jsonl"),
            "--report", str(tmp_path / "report.json"),
            "--run-id", "official-budget-required",
            "--official-mode",
        ],
    )

    with pytest.raises(SystemExit) as result:
        run_competition_batch.main()

    assert result.value.code == 2
    assert "requires evaluator values" in capsys.readouterr().err


def test_output_path_collision_is_rejected_before_any_overwrite(
    monkeypatch,
    tmp_path,
):
    org = valid_org_number(37_000_001)

    input_path, bulk_path = write_batch_files(
        tmp_path,
        [org],
    )

    original = input_path.read_bytes()

    output_path = tmp_path / "profiles.jsonl"
    report_path = tmp_path / "report.json"

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_competition_batch.py",
            "--organisations", str(input_path),
            "--bulk", str(bulk_path),
            "--output", str(input_path),
            "--profiles-output", str(output_path),
            "--report", str(report_path),
            "--run-id", "path-collision",
        ],
    )

    with pytest.raises(
        SystemExit,
        match="collides with input",
    ):
        run_competition_batch.main()

    assert input_path.read_bytes() == original
    assert not output_path.exists()
    assert not report_path.exists()


@pytest.mark.parametrize(
    "value",
    ["123456789", "12x609016", "1234"],
)
def test_invalid_organisation_numbers_are_rejected_before_enrichment(
    tmp_path,
    value,
):
    input_path = tmp_path / "invalid.jsonl"

    input_path.write_text(
        json.dumps({"organisation_number": value}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="Invalid Norwegian organisation number",
    ):
        read_organisation_inputs(input_path)


def test_duplicate_identifiers_are_rejected_after_normalization(
    tmp_path,
):
    input_path = tmp_path / "duplicate.jsonl"

    input_path.write_text(
        "\n".join([
            json.dumps({"organisation_number": "923609016"}),
            json.dumps({"organisation_number": "923 609 016"}),
        ]) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="contains duplicates",
    ):
        read_organisation_inputs(input_path)


def test_concurrent_batch_bounds_in_flight_work_and_preserves_input_order(
    monkeypatch,
    tmp_path,
):
    orgs = valid_org_numbers(40, 40_000_000)

    lock = threading.Lock()
    active = 0
    max_active = 0
    max_wait_set = 0

    real_wait = run_competition_batch.wait

    def tracking_wait(futures, return_when):
        nonlocal max_wait_set

        max_wait_set = max(
            max_wait_set,
            len(futures),
        )

        return real_wait(
            futures,
            return_when=return_when,
        )

    def fake_official(org, modules):
        nonlocal active, max_active

        with lock:
            active += 1
            max_active = max(max_active, active)

        time.sleep(
            0.005 * (int(org[-1]) % 3 + 1)
        )

        with lock:
            active -= 1

        return {
            "roles": evidence(
                "roles",
                "available",
                "official_roles",
                (
                    "https://data.brreg.no/enhetsregisteret/api/"
                    f"enheter/{org}/roller"
                ),
                value={"roles": []},
                content_sha256="a" * 64,
            )
        }, []

    monkeypatch.setattr(
        run_competition_batch,
        "wait",
        tracking_wait,
    )

    monkeypatch.setattr(
        run_competition_batch,
        "fetch_official_modules",
        fake_official,
    )

    rows, _ = invoke_batch(
        monkeypatch,
        tmp_path,
        orgs,
        modules="registry,roles",
        workers=4,
    )

    assert max_active <= 4
    assert max_active > 1
    assert max_wait_set <= 4
    assert len(rows) == 40

    assert [
        row["organisation_number"] for row in rows
    ] == orgs


class FakeResponse:
    status = 200

    def __init__(
        self,
        body: bytes,
        *,
        content_length: bool = True,
    ):
        self._stream = io.BytesIO(body)
        self.read_bytes = 0
        self.headers = {
            "Content-Type": "application/json",
        }

        if content_length:
            self.headers["Content-Length"] = str(len(body))

    def read(self, amount: int = -1) -> bytes:
        chunk = self._stream.read(amount)
        self.read_bytes += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def test_per_company_budget_metrics_use_thread_usage_deltas(
    monkeypatch,
    tmp_path,
):
    orgs = valid_org_numbers(3, 50_000_000)

    def fake_official(_org, _modules):
        budget = current_run_budget()
        assert budget is not None

        budget.before_request()

        body = budget.read_response(FakeResponse(b"abc"))
        assert body == b"abc"

        return {}, []

    monkeypatch.setattr(
        run_competition_batch,
        "fetch_official_modules",
        fake_official,
    )

    rows, report = invoke_batch(
        monkeypatch,
        tmp_path,
        orgs,
        modules="registry,roles",
        workers=1,
        extra_args=["--max-bytes", "100"],
    )

    profiles = [
        json.loads(line)
        for line in (tmp_path / "profiles.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]

    assert len(rows) == 3
    assert len(profiles) == 3

    # Public contract: per-company request counts.
    assert [
        row["operations"]["requests"] for row in rows
    ] == [1, 1, 1]

    # Internal profiles: per-company request and byte counts.
    assert [
        profile["run_metrics"]["requests"] for profile in profiles
    ] == [1, 1, 1]

    assert [
        profile["run_metrics"]["bytes"] for profile in profiles
    ] == [3, 3, 3]

    # Run-level totals.
    assert report["operations"]["requests"] == 3
    assert report["operations"]["bytes"] == 9

class FakeOpener:
    def __init__(self, response_factory):
        self.response_factory = response_factory
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        return self.response_factory()


def test_request_budget_exhaustion_stops_before_an_extra_request(
    monkeypatch,
):
    opener = FakeOpener(
        lambda: FakeResponse(b'{"ok":true}')
    )

    monkeypatch.setattr(
        "norway_company_agent.http.urllib.request.build_opener",
        lambda *_args, **_kwargs: opener,
    )

    budget = RunBudget(
        max_requests=1,
        max_bytes=100,
    )

    with use_run_budget(budget):
        assert fetch_json(
            "https://offline.test/resource"
        ).status == 200

        with pytest.raises(
            BudgetExceeded,
            match="requests budget exhausted",
        ):
            fetch_json(
                "https://offline.test/second"
            )

    assert opener.calls == 1
    assert budget.snapshot()["requests"] == 1


def test_byte_budget_exhaustion_never_reads_past_the_cap(
    monkeypatch,
):
    responses = []

    opener = FakeOpener(
        lambda: (
            responses.append(
                FakeResponse(
                    b'{"ok":true}',
                    content_length=False,
                )
            )
            or responses[-1]
        )
    )

    monkeypatch.setattr(
        "norway_company_agent.http.urllib.request.build_opener",
        lambda *_args, **_kwargs: opener,
    )

    budget = RunBudget(
        max_requests=2,
        max_bytes=4,
    )

    with (
        use_run_budget(budget),
        pytest.raises(
            BudgetExceeded,
            match="bytes budget exhausted",
        ),
    ):
        fetch_json(
            "https://offline.test/resource"
        )

    assert responses[0].read_bytes == 4
    assert budget.snapshot()["bytes_received"] == 4
    assert opener.calls == 1


@pytest.mark.parametrize(
    ("budget_args", "dimension"),
    [
        (["--max-requests", "0"], "requests"),
        (["--max-bytes", "1"], "bytes"),
        (["--max-runtime-seconds", "0"], "time"),
    ],
)
def test_batch_budget_exhaustion_keeps_terminal_result_and_report(
    monkeypatch,
    tmp_path,
    budget_args,
    dimension,
):
    org = "923609016"

    if dimension == "bytes":
        opener = FakeOpener(
            lambda: FakeResponse(
                b'{"ok":true}',
                content_length=False,
            )
        )

        monkeypatch.setattr(
            "norway_company_agent.http.urllib.request.build_opener",
            lambda *_args, **_kwargs: opener,
        )

    rows, report = invoke_batch(
        monkeypatch,
        tmp_path,
        [org],
        modules="registry,roles",
        extra_args=budget_args,
    )

    assert len(rows) == 1

    role_claim = next(
        claim
        for claim in rows[0]["claims"]
        if claim["field"] == "roles"
    )

    assert role_claim["availability"] == "failed"
    assert report["resource_budget"]["exhausted_by"] == dimension
    assert report["emitted_envelopes"] == 1
    assert report["validation"]["passed"] is True


@pytest.mark.parametrize(
    ("status", "expected_availability"),
    [
        ("source_error", "failed"),
        ("blocked", "blocked"),
    ],
)
def test_source_failures_and_blocked_sources_keep_their_availability(
    monkeypatch,
    tmp_path,
    status,
    expected_availability,
):
    org = "923609016"

    def fake_official(_org, _modules):
        return {
            "roles": evidence(
                "roles",
                status,
                "official_roles",
                "https://example.test/roles",
                note="mocked offline outcome",
            )
        }, []

    monkeypatch.setattr(
        run_competition_batch,
        "fetch_official_modules",
        fake_official,
    )

    rows, report = invoke_batch(
        monkeypatch,
        tmp_path,
        [org],
        modules="registry,roles",
    )

    claim = next(
        claim
        for claim in rows[0]["claims"]
        if claim["field"] == "roles"
    )

    assert claim["availability"] == expected_availability
    assert report["validation"]["passed"] is True
