"""Count failing results in ruleproof JSON reports, for the GitHub Action's `failed` output.

Usage: count_failed.py FAIL_ON STRICT REPORT [REPORT ...]

FAIL_ON is error, warning, info or never; STRICT is "true" or "false". Empty or missing
report paths are ignored. With FAIL_ON=never, every failing result is counted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

RANK = {"error": 0, "warning": 1, "info": 2, "never": 2}


def _severity(result: dict[str, Any]) -> str:
    rule = result.get("rule")
    if isinstance(rule, dict) and isinstance(rule.get("severity"), str):
        return str(rule["severity"])
    return str(result.get("severity", "error"))


def count(report: dict[str, Any], fail_on: str, strict: bool) -> int:
    bad = {"fail", "unverified"} if strict else {"fail"}
    limit = RANK[fail_on]
    results = report.get("results", [])
    if not isinstance(results, list):
        return 0
    return sum(
        1
        for r in results
        if isinstance(r, dict) and r.get("status") in bad and RANK.get(_severity(r), 0) <= limit
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in RANK:
        print(__doc__, file=sys.stderr)
        return 2
    fail_on, strict = argv[0], argv[1] == "true"
    total = 0
    for name in argv[2:]:
        if not name or not Path(name).is_file():
            continue
        try:
            data = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"warning: cannot read {name}: {exc}", file=sys.stderr)
            continue
        if isinstance(data, dict):
            total += count(data, fail_on, strict)
    print(total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
