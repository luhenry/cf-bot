#!/usr/bin/env python3
"""
Fetch tail of a CI build log given a check URL.

Supports:
  - GitHub Actions: https://github.com/<org>/<repo>/actions/runs/<runId>/job/<jobId>
  - Azure Pipelines: https://dev.azure.com/<org>/<project>/_build/results?buildId=<id>[&jobId=<jobId>]

Usage:
  python3 fetch_ci_log.py <check_url> [--lines N]

Prints the last N lines of the relevant log (default 80). Thin CLI shim over
cf_core.ci_log -- same usage as before this rearchitecture.
"""
import sys

from cf_core.ci_log import fetch_log


def main():
    args = sys.argv[1:]
    lines = 80
    if "--lines" in args:
        idx = args.index("--lines")
        lines = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]
    if not args:
        print("usage: fetch_ci_log.py <url> [--lines N]", file=sys.stderr)
        sys.exit(1)
    print(fetch_log(args[0], lines))


if __name__ == "__main__":
    main()
