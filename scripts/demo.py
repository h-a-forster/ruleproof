"""Reproduce the sample run in README.md.

Usage: uv run python scripts/demo.py [--keep]

Copies examples/python-service to a temporary directory, commits a baseline, applies an agent's
changes, writes a scripted Claude Code session next to it (session.jsonl) and runs
`ruleproof -q check --transcript ../session.jsonl` in the copy. Standard library only. With
--keep, the temporary directory is left in place and its path is printed.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "python-service"

# The service code the instruction files talk about, committed as the baseline.
BASELINE = {
    "orders/__init__.py": '"""Orders service."""\n',
    "orders/models/order.py": '"""Order model."""\n\nclass Order:\n    total: int\n',
    "orders/service.py": '"""Order service."""\n\n\ndef place(order):\n    return order\n',
    "orders/clients/payments/api.py": "# generated\nclass PaymentsApi: ...\n",
    "migrations/versions/001_init.py": '"""init"""\n',
    "tests/test_service.py": "def test_ok():\n    assert True\n",
}
# What the agent changed: it hand-edits the generated client and the service.
AGENT_CHANGES = {
    "orders/clients/payments/api.py": "# generated\nclass PaymentsApi:\n    timeout = 30\n",
    "orders/service.py": (
        '"""Order service."""\n\n\ndef place(order, timeout=30):\n    return order\n'
    ),
}


def write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def git(repo: Path, *args: str) -> None:
    config = ["-c", "user.name=demo", "-c", "user.email=demo@example.com"]
    config += ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull]
    subprocess.run(["git", *config, *args], cwd=repo, check=True, capture_output=True)


def session(repo: Path) -> list[dict[str, Any]]:
    """A Claude Code session: edit, test, edit again, pip install, then claim the tests pass."""
    root = str(repo)
    records: list[dict[str, Any]] = []

    def rec(kind: str, content: list[dict[str, Any]]) -> None:
        n = len(records) + 1
        records.append(
            {
                "type": kind,
                "uuid": f"u{n}",
                "sessionId": "demo-session",
                "cwd": root,
                "timestamp": f"2026-10-09T10:00:{n:02d}Z",
                "message": {"role": kind, "content": content},
            }
        )

    def tool(name: str, inp: dict[str, Any], result: str) -> None:
        tid = f"t{len(records)}"
        rec("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": inp}])
        rec(
            "user",
            [{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": False}],
        )

    def edit(rel: str) -> dict[str, Any]:
        return {"file_path": f"{root}/{rel}", "old_string": "a", "new_string": "b"}

    rec("user", [{"type": "text", "text": "Add a payment timeout to order placement."}])
    tool("Edit", edit("orders/clients/payments/api.py"), "ok")
    tool("Bash", {"command": "uv run pytest -q"}, "1 passed in 0.02s")
    tool("Edit", edit("orders/service.py"), "ok")
    tool("Bash", {"command": "pip install requests"}, "Successfully installed requests-2.32.5")
    done = "Done. `place()` now takes a timeout and passes it to the payments client. "
    rec("assistant", [{"type": "text", "text": done + "All tests pass."}])
    return records


def build(workdir: Path) -> Path:
    """The demo repo at ``workdir/python-service``, with ``workdir/session.jsonl`` next to it."""
    repo = workdir / "python-service"
    shutil.copytree(EXAMPLE, repo)
    write(repo, BASELINE)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    write(repo, AGENT_CHANGES)
    lines = [json.dumps(r) for r in session(repo)]
    (workdir / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return repo


def _make_writable(func: Any, path: str, _exc: Any) -> None:
    # Git writes its objects read-only; Windows refuses to delete read-only files.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable)
    else:
        shutil.rmtree(path, onerror=_make_writable)


def main(argv: list[str]) -> int:
    keep = "--keep" in argv
    workdir = Path(tempfile.mkdtemp(prefix="ruleproof-demo-"))
    try:
        repo = build(workdir)
        args = ["-q", "check", "--transcript", "../session.jsonl"]
        print("$ ruleproof " + " ".join(args), flush=True)
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        code = subprocess.run([sys.executable, "-m", "ruleproof", *args], cwd=repo, env=env)
        if keep:
            print(f"\nDemo repo: {repo}\nTranscript: {workdir / 'session.jsonl'}")
        return 0 if code.returncode in (0, 1) else code.returncode  # 1: rules failed, as intended
    finally:
        if not keep:
            remove(workdir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
