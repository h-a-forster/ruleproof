"""Count failing results in ruleproof JSON reports, for the GitHub Action's `failed` output.

Usage: count_failed.py FAIL_ON STRICT REPORT [REPORT ...]

FAIL_ON is error, warning, info or never; STRICT is "true" or "false". Empty report arguments
are ignored (a step that did not run). Prints two numbers: the failing results at or above
FAIL_ON (all of them with "never"), and the failing results in total. With STRICT,
`unverified` counts as failing. Exits 1 with a message when a report is missing or invalid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

RANK = {"error": 0, "warning": 1, "info": 2, "never": 2}
SCHEMA = "ruleproof/report@1"


class ReportError(Exception):
    pass


def load(name: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReportError(f"report {name} does not exist") from None
    except (OSError, ValueError) as exc:
        raise ReportError(f"cannot read report {name}: {exc}") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ReportError(f"report {name} is not a {SCHEMA} JSON report")
    results = data.get("results")
    if not isinstance(results, list):
        raise ReportError(f"report {name} has no results list")
    return [r for r in results if isinstance(r, dict)]


def count(results: list[dict[str, Any]], fail_on: str, strict: bool) -> tuple[int, int]:
    bad = {"fail", "unverified"} if strict else {"fail"}
    failing = [r for r in results if r.get("status") in bad]
    limit = RANK[fail_on]
    at_level = sum(1 for r in failing if RANK.get(str(r.get("severity")), 0) <= limit)
    return at_level, len(failing)


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in RANK or argv[1] not in ("true", "false"):
        print(__doc__, file=sys.stderr)
        return 2
    fail_on, strict = argv[0], argv[1] == "true"
    at_level = total = 0
    for name in argv[2:]:
        if not name:
            continue
        try:
            a, t = count(load(name), fail_on, strict)
        except ReportError as exc:
            print(f"::error title=ruleproof::{exc}")
            return 1
        at_level += a
        total += t
    print(at_level, total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
