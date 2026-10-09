"""Regression tests for bugs found by reviewing the parsers against real transcripts."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ruleproof.errors import TranscriptError
from ruleproof.models import EventKind, Session
from ruleproof.transcripts import detect_agent, load_session
from ruleproof.transcripts._codemode import parse_cell
from ruleproof.transcripts._shell import unwrap
from ruleproof.transcripts.discover import find_sessions, native_path, resolve_session
from ruleproof.transcripts.export import to_json, write_sqlite

BS = chr(92)
NL = BS + "n"  # a JavaScript / JSON newline escape


def _jsonl(path: Path, *records: object) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


# --- shell unwrapping ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "bash -lc 'pytest -q' && git push --force origin main",
            "pytest -q && git push --force origin main",
        ),
        (["bash", "-c", "echo $0", "arg0"], "echo $0 arg0"),
        ("bash -c -- 'x'", "x"),
        (["bash", "-c", "--", "x"], "x"),
        ("bash -c ''", ""),
        ([], ""),
        ("env FOO=1 BAR=2 bash -lc 'make test'", "make test"),
        ("sudo -u root bash -c 'id'", "id"),
        ("nohup sh -c 'serve' &", "serve &"),
        ("time bash -c pytest", "pytest"),
        ("command -p bash -c ls", "ls"),
        ("sudo pip install x", "sudo pip install x"),  # no shell follows: left alone
        ("env FOO=1 pytest", "env FOO=1 pytest"),
        (f"C:{BS}Program Files{BS}Git{BS}bin{BS}bash.exe -c 'git status'", "git status"),
        (f"C:{BS}Program Files{BS}tool.exe -c x", f"C:{BS}Program Files{BS}tool.exe -c x"),
    ],
)
def test_unwrap_keeps_tail_and_skips_prefixes(command: str | list[str], expected: str) -> None:
    assert unwrap(command) == expected


# --- code mode -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("js", "command"),
    [
        ('tools.exec_command({env: {cmd: "fake"}, cmd: "git push"})', "git push"),
        ('tools.exec_command({cmd: "git " + "push"})', "git push"),
        ('const b = "git"; tools.exec_command({cmd: b + " push"})', "git push"),
        ('const c = ["git", "status"].join(" "); tools.exec_command({cmd: c})', "git status"),
        ("tools.exec_command({cmd: base + ' push'})", None),
        ("tools.exec_command({cmd: `ls ${dir}`})", None),
        ("tools.exec_command({cmd: pick(x)})", None),
        ('tools.exec_command({env: {cmd: "fake"}})', None),
    ],
)
def test_code_mode_command_resolution(js: str, command: str | None) -> None:
    (call,) = parse_cell(js).calls
    assert call.command == command


def test_code_mode_patches(tmp_path: Path) -> None:
    joined = (
        'const p = ["*** Begin Patch", "*** Add File: a.py", "+x", "*** End Patch"].join("'
        + NL
        + '"); await tools.apply_patch(p);'
    )
    by_key = (
        'await tools.apply_patch({patch: "*** Begin Patch'
        + NL
        + "*** Delete File: b.py"
        + NL
        + '*** End Patch"});'
    )
    decoy = 'const doc = "*** Add File: decoy.py"; await tools.apply_patch(makePatch());'
    cmd_decoy = 'await tools.exec_command({cmd: "echo *** Update File: nope.py"});'
    records = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": "s"}},
    ]
    for i, js in enumerate(
        (joined, by_key, decoy, cmd_decoy, "await tools.exec_command({cmd: c});")
    ):
        payload = {"type": "custom_tool_call", "name": "exec", "call_id": f"c{i}", "input": js}
        records.append(
            {
                "timestamp": f"2026-01-01T00:00:0{i + 1}Z",
                "type": "response_item",
                "payload": payload,
            }
        )
    s = load_session(_jsonl(tmp_path / "r.jsonl", *records))
    assert [(e.action, e.path) for e in s.edits()] == [("add", "a.py"), ("delete", "b.py")]
    tools = [(e.tool, e.text.split(":")[0]) for e in s.of_kind(EventKind.TOOL)]
    assert tools == [("apply_patch", "unresolved patch"), ("exec_command", "unresolved command")]
    assert [c.text for c in s.commands()] == ["echo *** Update File: nope.py"]


# --- robustness ----------------------------------------------------------------------------


def test_empty_argv_and_odd_records_never_crash(tmp_path: Path) -> None:
    claude = _jsonl(
        tmp_path / "c.jsonl",
        {
            "type": "assistant",
            "sessionId": "s",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": []}}
                ]
            },
        },
    )
    assert load_session(claude).commands()[0].text == ""
    codex = _jsonl(
        tmp_path / "x.jsonl",
        {"timestamp": "t", "type": "session_meta", "payload": {"id": "s"}},
        {
            "timestamp": "t",
            "type": "response_item",
            "payload": {"type": "local_shell_call", "call_id": "a", "action": {"command": []}},
        },
        {
            "timestamp": "t",
            "type": "event_msg",
            "payload": {"type": "exec_command_end", "call_id": "zz", "command": []},
        },
        {
            "timestamp": "t",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "CommandExecution", "command": [], "exit_code": 0},
            },
        },
        {
            "timestamp": "t",
            "type": "response_item",
            "payload": {"type": "web_search_call", "action": "not-a-dict"},
        },
        {"type": "item.completed", "item": {"type": "command_execution", "command": []}},
    )
    s = load_session(codex)
    assert [c.text for c in s.commands()] == ["", ""]
    assert any("malformed response_item" in w for w in s.warnings)


def test_surrogates_are_paired_or_replaced(tmp_path: Path) -> None:
    lone = BS + "ud83d"
    line = (
        '{"type":"user","sessionId":"s","timestamp":"2026-01-01T00:00:00Z",'
        f'"message":{{"content":"bad {lone} char"}}}}'
    )
    path = tmp_path / "c.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    s = load_session(path)
    assert s.events[0].text == "bad " + chr(0xFFFD) + " char"
    to_json(s).encode("utf-8")

    pair = BS + "uD83D" + BS + "uDE00"
    (call,) = parse_cell(f'tools.exec_command({{cmd: "echo {pair}"}})').calls
    codex = _jsonl(
        tmp_path / "x.jsonl",
        {"timestamp": "t", "type": "session_meta", "payload": {"id": "s"}},
        {
            "timestamp": "t",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c",
                "input": f'tools.exec_command({{cmd: "echo {pair} {BS}uDC00"}})',
            },
        },
    )
    assert call.command is not None
    text = load_session(codex).commands()[0].text
    assert text == "echo " + chr(0x1F600) + " " + chr(0xFFFD)
    text.encode("utf-8")


def test_codex_items_ordered_by_start(tmp_path: Path) -> None:
    def item(ts: str, started_ms: int, body: dict[str, object]) -> dict[str, object]:
        payload = {"type": "item_completed", "started_at_ms": started_ms, "item": body}
        return {"timestamp": ts, "type": "event_msg", "payload": payload}

    cmd = {"type": "CommandExecution", "command": ["pwsh", "-Command", "pytest"], "exit_code": 0}
    edit = {"type": "FileChange", "changes": {"a.py": {"type": "update"}}}
    path = _jsonl(
        tmp_path / "r.jsonl",
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": "s"}},
        # the edit finishes first, but the test run started before it
        item("2026-01-01T00:00:05Z", 1767225602000, edit),
        item("2026-01-01T00:00:09Z", 1767225601000, cmd),
    )
    s = load_session(path)
    assert [e.kind for e in s.events] == [EventKind.COMMAND, EventKind.EDIT]
    assert [e.index for e in s.events] == [0, 1]


def test_detection_with_first_line_over_one_megabyte(tmp_path: Path) -> None:
    big = {"type": "queue-operation", "sessionId": "s", "content": "x" * (1 << 21)}
    user = {
        "type": "user",
        "sessionId": "s",
        "cwd": "/w",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"content": "hi"},
    }
    path = _jsonl(tmp_path / "s.jsonl", big, user)
    assert detect_agent(path) == "claude-code"
    assert [e.text for e in load_session(path).events] == ["hi"]
    meta = {
        "timestamp": "t",
        "type": "session_meta",
        "payload": {"id": "c", "cwd": "/w", "base_instructions": "y" * (1 << 21)},
    }
    assert detect_agent(_jsonl(tmp_path / "r.jsonl", meta)) == "codex"


# --- discovery and export ------------------------------------------------------------------


def test_discovery_reads_long_first_lines_and_msys_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gemini"))
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    rollout = tmp_path / "codex" / "sessions" / "rollout-2026-01-01T00-00-00-x.jsonl"
    rollout.parent.mkdir(parents=True)
    meta = {"id": "abc", "cwd": str(repo), "base_instructions": "y" * (1 << 21)}
    _jsonl(rollout, {"timestamp": "t", "type": "session_meta", "payload": meta})
    assert [r.id for r in find_sessions(repo)] == ["abc"]
    if os.name == "nt":
        msys = "/" + str(repo)[0].lower() + str(repo)[2:].replace(BS, "/")
        assert native_path(msys).lower() == str(repo).replace(BS, "/").lower()
        assert [r.id for r in find_sessions(Path(msys))] == ["abc"]
        msys_rollout = "/" + str(rollout)[0].lower() + str(rollout)[2:].replace(BS, "/")
        assert resolve_session(msys_rollout, repo).id == "abc"
    else:
        assert native_path("/c/Users/x") == "/c/Users/x"
    with pytest.raises(TranscriptError, match="empty session id"):
        resolve_session("  ", repo)


def test_write_sqlite_keeps_sessions_with_the_same_id(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    a = Session("codex", "same", "/a.jsonl")
    b = Session("codex", "same", "/b.jsonl")
    write_sqlite([a, b], db)
    write_sqlite([a], db)
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT id, path FROM sessions ORDER BY path").fetchall()
    assert rows == [("same", "/a.jsonl"), ("same", "/b.jsonl")]
