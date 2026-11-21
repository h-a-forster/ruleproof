"""Time the Claude Code hooks end to end on large synthetic transcripts.

    uv run python scripts/bench_hook_transcript.py --mb 200 1000

For each size it writes a synthetic Claude Code transcript under ``tmp/hookbench/`` (a seeded,
append-only mix of prompts, assistant text, thinking blocks, shell commands, edits, file reads
and tool results from a few hundred bytes to a few hundred kB), then starts ``ruleproof hook
claude-pretool`` and ``ruleproof hook claude-stop`` as subprocesses and reports the wall
time of each call in a series (``--calls``, default 10): the first starts with no cache, so
the whole file is read, and every later one follows an append of a few records, which is what
a hook call in a live session sees. The numbers in ``docs/claude-code-hook.md`` come from this
script; for the numbers before incremental reads, run it with ``PYTHONPATH`` set to the
``src`` folder of an older checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "tmp" / "hookbench"
SESSION = "5e55105e-0000-4000-8000-000000000001"
RULES = """\
version = 1

[[rule]]
id = "no-push"
description = "Never push unless the user asks."
check = "forbid-command"
command = '\\bgit\\s+push\\b'
unless_user = '\\bpush\\b'

[[rule]]
id = "tests-pass"
description = "Run pytest after the last edit; it must pass."
check = "require-command"
command = '\\bpytest\\b'

[[rule]]
id = "honest-claims"
description = "Do not claim results you did not verify."
check = "claims"
"""
_WORDS = (
    "the",
    "parser",
    "reads",
    "each",
    "record",
    "and",
    "keeps",
    "the",
    "state",
    "the",
    "rules",
    "need",
    "while",
    "the",
    "tests",
    "cover",
    "appends",
    "truncation",
    "and",
    "torn",
    "lines",
    "so",
    "results",
    "match",
)


class Writer:
    """Appends realistic Claude Code records to an open transcript."""

    def __init__(self, fh: TextIO, seed: int, cwd: str) -> None:
        self.fh, self.rng, self.cwd = fh, random.Random(seed), cwd
        self.n = 0
        self.parent: str | None = None
        self.written = 0

    def _text(self, chars: int) -> str:
        words: list[str] = []
        size = 0
        while size < chars:
            w = self.rng.choice(_WORDS)
            words.append(w)
            size += len(w) + 1
        return " ".join(words)

    def _emit(self, rec: dict[str, Any]) -> None:
        self.n += 1
        uuid = f"00000000-0000-4000-8000-{self.n:012d}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_790_000_000 + self.n))
        base = {
            "parentUuid": self.parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": self.cwd,
            "sessionId": SESSION,
            "version": "2.1.0",
            "gitBranch": "main",
            "uuid": uuid,
            "timestamp": stamp + ".000Z",
        }
        line = json.dumps({**base, **rec}) + "\n"
        self.fh.write(line)
        self.written += len(line)
        self.parent = uuid

    def user(self, text: str) -> None:
        self._emit({"type": "user", "message": {"role": "user", "content": text}})

    def _assistant(self, block: dict[str, Any]) -> None:
        msg = {
            "id": f"msg_{self.n:012d}",
            "type": "message",
            "role": "assistant",
            "model": "claude-synthetic",
            "content": [block],
            "usage": {"input_tokens": 3, "output_tokens": 180, "cache_read_input_tokens": 90000},
        }
        self._emit({"type": "assistant", "requestId": f"req_{self.n:012d}", "message": msg})

    def say(self, chars: int = 400) -> None:
        self._assistant({"type": "text", "text": self._text(chars)})

    def think(self) -> None:
        sig = "".join(self.rng.choices("ABCDEFGHabcdefgh0123456789+/", k=600))
        block = {"type": "thinking", "thinking": self._text(900), "signature": sig}
        self._assistant(block)

    def tool(self, name: str, inp: dict[str, Any], result: str, extra: Any) -> None:
        tid = f"toolu_{self.n:012d}"
        self._assistant({"type": "tool_use", "id": tid, "name": name, "input": inp})
        block = {"type": "tool_result", "tool_use_id": tid, "content": result}
        msg = {"role": "user", "content": [block]}
        self._emit({"type": "user", "message": msg, "toolUseResult": extra})

    def turn(self) -> None:
        """One step of agent work: some thinking, a tool call or two, sometimes prose."""
        rng = self.rng
        if rng.random() < 0.5:
            self.think()
        roll = rng.random()
        if roll < 0.40:
            big = rng.random() < 0.15
            out = self._text(rng.randint(20_000, 120_000) if big else rng.randint(100, 3000))
            cmd = rng.choice(["uv run pytest -q", "git status", "ls src", "uv run ruff check ."])
            extra = {"stdout": out, "stderr": "", "interrupted": False}
            self.tool("Bash", {"command": cmd, "description": "run it"}, out, extra)
        elif roll < 0.70:
            big = rng.random() < 0.25
            body = self._text(rng.randint(60_000, 400_000) if big else rng.randint(2000, 20_000))
            path = f"{self.cwd}/src/mod_{rng.randint(1, 60)}.py"
            extra = {"type": "text", "file": {"filePath": path, "content": body}}
            self.tool("Read", {"file_path": path}, body, extra)
        elif roll < 0.88:
            path = f"{self.cwd}/src/mod_{rng.randint(1, 60)}.py"
            old, new = self._text(300), self._text(600)
            inp = {"file_path": path, "old_string": old, "new_string": new}
            extra = {"filePath": path, "oldString": old, "newString": new, "originalFile": ""}
            self.tool("Edit", inp, f"The file {path} has been updated successfully.", extra)
        else:
            out = self._text(rng.randint(500, 8000))
            self.tool("Grep", {"pattern": "def parse", "path": "src"}, out, {"content": out})
        if rng.random() < 0.3:
            self.say()
        if rng.random() < 0.02:
            self.user(self._text(200))


def generate(path: Path, mb: int, cwd: str) -> None:
    target = mb * 1_000_000
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        w = Writer(fh, seed=mb, cwd=cwd)
        w.user("Fix the parser and keep the tests green.")
        while w.written < target:
            w.turn()
        w.say()


def append_turns(path: Path, cwd: str, seed: int, turns: int = 3) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        w = Writer(fh, seed=seed, cwd=cwd)
        w.n = 10_000_000 * seed
        for _ in range(turns):
            w.turn()
        w.say()


def run_hook(name: str, payload: dict[str, Any], env: dict[str, str]) -> tuple[float, str, str]:
    cmd = [sys.executable, "-m", "ruleproof", "hook", name, *HOOK_ARGS]
    start = time.perf_counter()
    proc = subprocess.run(
        cmd, input=json.dumps(payload), capture_output=True, text=True, env=env, check=False
    )
    return time.perf_counter() - start, proc.stdout.strip(), proc.stderr.strip()


HOOK_ARGS: list[str] = []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mb", type=int, nargs="+", default=[200, 1000])
    ap.add_argument("--calls", type=int, default=10, help="hook calls per series (10)")
    ap.add_argument("--keep", action="store_true", help="keep the generated transcripts")
    ap.add_argument("hook_args", nargs="*", help="extra arguments for both hooks, after --")
    args = ap.parse_args()
    HOOK_ARGS.extend(args.hook_args)

    repo = WORK / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "ruleproof.toml").write_text(RULES, encoding="utf-8")
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = {**os.environ, "RULEPROOF_CACHE_DIR": str(WORK / "cache")}

    for mb in args.mb:
        path = WORK / f"session-{mb}mb.jsonl"
        if not path.exists():
            generate(path, mb, repo.as_posix())
        shutil.rmtree(WORK / "cache", ignore_errors=True)
        base = {"session_id": SESSION, "transcript_path": str(path), "cwd": str(repo)}
        pre = {
            **base,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
        }
        stop = {**base, "hook_event_name": "Stop", "stop_hook_active": False}
        size = path.stat().st_size / 1e6
        sys.stdout.write(f"\n{mb} MB transcript ({size:.0f} MB on disk before appends)\n")
        for hook, payload in (("claude-pretool", pre), ("claude-stop", stop)):
            shutil.rmtree(WORK / "cache", ignore_errors=True)
            times: list[float] = []
            for call in range(1, args.calls + 1):
                if call > 1:
                    append_turns(path, repo.as_posix(), seed=mb + call + len(hook))
                secs, out, err = run_hook(hook, payload, env)
                times.append(secs)
                decided = "decision" if out else "no decision"
                note = f"  [{err.splitlines()[-1][:150]}]" if err else ""
                label = "no cache" if call == 1 else "after append"
                sys.stdout.write(
                    f"  {hook:15} call {call:2} {label:13} {secs:7.2f} s  {decided}{note}\n"
                )
                sys.stdout.flush()
            if len(times) > 1:
                later = sorted(times[1:])
                sys.stdout.write(
                    f"  {hook:15} calls 2-{len(times)}: median {statistics.median(later):.2f} s, "
                    f"min {later[0]:.2f} s, max {later[-1]:.2f} s\n"
                )
            cache = WORK / "cache"
            if cache.exists():
                used = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file()) / 1e6
                sys.stdout.write(f"  {hook:15} cache on disk: {used:.1f} MB\n")
        if not args.keep:
            path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
