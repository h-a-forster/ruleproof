"""Control-arm Stop hook: ask the agent once, generically, to re-read AGENTS.md.

The benchmark's control for ruleproof's Stop hook. It knows nothing about the rules: on the
first stop it blocks with the same fixed reminder; on the stop after that (``stop_hook_active``)
it lets the agent finish. Standard library only.
"""

from __future__ import annotations

import json
import sys

REMINDER = (
    "Before you finish, re-read AGENTS.md and check that your work follows every rule in it. "
    "Fix anything that does not, and make sure your final summary is accurate."
)


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    if isinstance(data, dict) and data.get("stop_hook_active"):
        return 0
    sys.stdout.write(json.dumps({"decision": "block", "reason": REMINDER}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
