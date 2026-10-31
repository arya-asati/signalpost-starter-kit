
from __future__ import annotations

import json
import os
import runpy
import socket
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from urllib3.connectionpool import HTTPConnectionPool


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_competition_batch.py"

counts = Counter()
hosts = Counter()


def record_request(client: str, url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(str(url))
        if parsed.scheme.lower() not in {"http", "https"}:
            return

        counts[client] += 1
        hosts[f"{client}: {parsed.hostname or 'unknown'}"] += 1
    except ValueError:
        return


# Instrument urllib requests, including recursive redirect opens.
original_opener_open = urllib.request.OpenerDirector.open


def counted_opener_open(
    self,
    fullurl,
    data=None,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
):
    url = (
        fullurl.full_url
        if isinstance(fullurl, urllib.request.Request)
        else str(fullurl)
    )
    record_request("urllib", url)

    return original_opener_open(
        self, fullurl, data=data, timeout=timeout
    )


# Instrument urllib3's HTTP connection pool.
# This also observes requests-library traffic, including redirects.
original_pool_urlopen = HTTPConnectionPool.urlopen


def counted_pool_urlopen(self, method, url, *args, **kwargs):
    request_url = str(url)

    if not urllib.parse.urlsplit(request_url).scheme:
        request_url = (
            f"{self.scheme}://{self.host}/"
            + request_url.lstrip("/")
        )

    record_request("urllib3", request_url)

    return original_pool_urlopen(
        self, method, url, *args, **kwargs
    )


def find_report_argument(arguments):
    for index, argument in enumerate(arguments):
        if argument == "--report" and index + 1 < len(arguments):
            return arguments[index + 1]

        if argument.startswith("--report="):
            return argument.split("=", 1)[1]

    return None


def main():
    if not RUNNER.exists():
        raise SystemExit(f"Batch runner not found: {RUNNER}")

    runner_args = sys.argv[1:]
    report_value = find_report_argument(runner_args)

    if not report_value:
        raise SystemExit("Please supply the normal --report argument.")

    report_path = Path(report_value)
    if not report_path.is_absolute():
        report_path = ROOT / report_path

    old_argv = sys.argv[:]
    exit_code = 0

    urllib.request.OpenerDirector.open = counted_opener_open
    HTTPConnectionPool.urlopen = counted_pool_urlopen

    try:
        # Force an empty tldextract cache to expose hidden first-use traffic.
        with tempfile.TemporaryDirectory(
            prefix="signalpost-cold-request-audit-"
        ) as cold_cache:
            os.environ["TLDEXTRACT_CACHE"] = cold_cache
            os.environ["TLDEXTRACT_CACHE_TIMEOUT"] = "3"
            os.environ.pop(
                "TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS", None
            )

            sys.argv = [str(RUNNER), *runner_args]

            try:
                runpy.run_path(
                    str(RUNNER),
                    run_name="__main__",
                )
            except SystemExit as exc:
                if isinstance(exc.code, int):
                    exit_code = exc.code
                elif exc.code is not None:
                    print("Batch runner exit:", exc.code)
                    exit_code = 1

    finally:
        urllib.request.OpenerDirector.open = original_opener_open
        HTTPConnectionPool.urlopen = original_pool_urlopen
        sys.argv = old_argv

    print("\n" + "=" * 55)
    print("INDEPENDENT NETWORK REQUEST AUDIT")
    print("=" * 55)

    urllib_count = counts["urllib"]
    urllib3_count = counts["urllib3"]
    independent_total = urllib_count + urllib3_count

    print("urllib transport calls:", urllib_count)
    print("urllib3 transport calls:", urllib3_count)
    print("Independent HTTP request attempts:", independent_total)

    print("\nRequests by host:")
    for host, count in hosts.most_common(20):
        print(f"  {host}: {count}")

    if report_path.exists():
        report = json.loads(
            report_path.read_text(encoding="utf-8")
        )
        reported = report.get("operations", {}).get("requests")

        print("\nApplication report requests:", reported)

        if isinstance(reported, int):
            difference = independent_total - reported
            print("Independent minus reported:", difference)
            print("Counts match:", independent_total == reported)
        else:
            print("Counts match: UNKNOWN (report count missing)")
    else:
        print("\nReport file not found:", report_path)

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
