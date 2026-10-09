from __future__ import annotations

import json
from pathlib import Path

import pytest

from ruleproof.errors import TranscriptError
from ruleproof.models import OUTPUT_LIMIT, EventKind, Session
from ruleproof.transcripts import detect_agent, load_session

FIX = Path(__file__).parent / "fixtures" / "transcripts"
CLAUDE = FIX / "claude" / "11111111-2222-3333-4444-555555555555.jsonl"
BOM = chr(0xFEFF)


def kinds(session: Session) -> list[str]:
    return [e.kind.value for e in session.events]


def assert_indexed(session: Session) -> None:
    assert [e.index for e in session.events] == list(range(len(session.events)))


@pytest.mark.parametrize(
    ("name", "agent"),
    [
        ("claude/11111111-2222-3333-4444-555555555555.jsonl", "claude-code"),
        ("claude_stream.jsonl", "claude-code"),
        ("codex_old.jsonl", "codex"),
        ("codex_codemode.jsonl", "codex"),
        ("codex_items.jsonl", "codex"),
        ("codex_exec.jsonl", "codex"),
        ("gemini_session.json", "gemini-cli"),
        ("generic.jsonl", "generic"),
    ],
)
def test_detect_agent(name: str, agent: str) -> None:
    assert detect_agent(FIX / name) == agent
    session = load_session(FIX / name)
    assert session.events
    assert_indexed(session)


# --- Claude Code -------------------------------------------------------------------------


def test_claude_transcript_with_subagent() -> None:
    s = load_session(CLAUDE)
    assert (s.agent, s.id, s.cwd, s.model) == (
        "claude-code",
        "11111111-2222-3333-4444-555555555555",
        "C:\\work\\demo",
        "claude-test-1",
    )
    assert s.started_at == "2026-01-02T10:00:00.000Z"
    assert_indexed(s)
    assert kinds(s) == [
        "user",
        "assistant",
        "tool",
        "edit",
        "edit",
        "command",
        "tool",
        "user",
        "edit",
        "command",
        "assistant",
        "command",
        "command",
        "assistant",
    ]
    assert s.events[0].text == "Add a slugify helper and run the tests."  # isMeta skipped
    read, write, edit = s.events[2], s.events[3], s.events[4]
    assert (read.tool, read.text) == ("Read", "C:\\work\\demo\\README.md")
    assert (write.path, write.action) == ("C:\\work\\demo\\slug.py", "add")
    assert (edit.path, edit.action) == ("C:\\work\\demo\\app.py", "modify")

    failed, sub_cmd, ps, background = s.commands()
    assert (failed.text, failed.exit_code, failed.is_error) == ("uv run pytest -q", 1, True)
    assert failed.output.startswith("Exit code 1")
    assert (sub_cmd.actor, sub_cmd.exit_code) == ("a0b1c2", 0)
    assert (ps.tool, ps.exit_code, ps.output) == ("PowerShell", 0, "4 passed")
    assert (background.text, background.exit_code) == ("npm run dev", None)

    sub_events = [e for e in s.events if e.actor == "a0b1c2"]
    assert [e.index for e in sub_events] == [7, 8, 9, 10]
    final = s.final_message()
    assert final is not None and final.text == "Done: all 4 tests pass."
    assert any(w.startswith("line 21: invalid JSON") for w in s.warnings)


def test_claude_without_subagents() -> None:
    s = load_session(CLAUDE, include_subagents=False)
    assert {e.actor for e in s.events} == {"main"}
    assert len(s.events) == 10


def test_claude_stream_json() -> None:
    s = load_session(FIX / "claude_stream.jsonl")
    assert (s.id, s.cwd, s.model) == ("stream-0001", "/home/dev/demo", "claude-test-2")
    cmd, edit, msg = s.events
    assert (cmd.text, cmd.exit_code, cmd.is_error) == ("pytest -q", 2, True)
    assert (edit.tool, edit.action, edit.actor) == ("MultiEdit", "modify", "toolu_parent")
    assert msg.text == "Tests fail: no tests collected."


def test_claude_stream_json_array(tmp_path: Path) -> None:
    records = [
        json.loads(line) for line in (FIX / "claude_stream.jsonl").read_text("utf-8").splitlines()
    ]
    path = tmp_path / "out.json"
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    assert detect_agent(path) == "claude-code"
    s = load_session(path)
    assert kinds(s) == ["command", "edit", "assistant"]
    assert s.id == "stream-0001"


def test_claude_crlf_and_bom(tmp_path: Path) -> None:
    text = CLAUDE.read_text(encoding="utf-8").replace("\n", "\r\n")
    path = tmp_path / "s.jsonl"
    path.write_bytes((BOM + text).encode("utf-8"))
    s = load_session(path)
    assert len(s.events) == 10
    assert s.commands()[0].exit_code == 1


def _claude_lines(*records: dict[str, object]) -> str:
    return "\n".join(json.dumps(r) for r in records) + "\n"


def _tool_use(tid: str, name: str, inp: dict[str, object]) -> dict[str, object]:
    return {
        "type": "assistant",
        "sessionId": "s1",
        "message": {
            "id": tid,
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
        },
    }


def _tool_result(
    tid: str, content: str, is_error: bool = False, extra: object = None
) -> dict[str, object]:
    rec: dict[str, object] = {
        "type": "user",
        "sessionId": "s1",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": content,
                    "is_error": is_error,
                }
            ]
        },
    }
    if extra is not None:
        rec["toolUseResult"] = extra
    return rec


def test_claude_result_details(tmp_path: Path) -> None:
    long_output = "HEAD" + "x" * (OUTPUT_LIMIT * 2) + "TAIL"
    path = tmp_path / "s.jsonl"
    path.write_text(
        _claude_lines(
            _tool_use("w1", "Write", {"file_path": "a.py"}),
            _tool_result("w1", "File created successfully at: a.py"),
            _tool_use("w2", "Write", {"file_path": "b.py"}),
            _tool_result("w2", "The file b.py has been updated successfully."),
            _tool_use("w3", "Write", {"file_path": "c.py"}),
            _tool_result("w3", "<tool_use_error>denied</tool_use_error>", is_error=True),
            _tool_use("b1", "Bash", {"command": "sleep 100"}),
            _tool_result("b1", "", extra={"stdout": "", "interrupted": True}),
            _tool_use("b2", "Bash", {"command": "make"}),
            _tool_result("b2", "<tool_use_error>blocked</tool_use_error>", is_error=True),
            _tool_use("b3", "Bash", {"command": "cat big"}),
            _tool_result("b3", long_output),
            _tool_use("n1", "NotebookEdit", {"notebook_path": "nb.ipynb"}),
            _tool_use("b3", "Bash", {"command": "cat big"}),  # re-emitted block is ignored
        ),
        encoding="utf-8",
    )
    s = load_session(path)
    assert [(e.path, e.action) for e in s.edits()] == [
        ("a.py", "add"),
        ("b.py", "modify"),
        ("c.py", "unknown"),
        ("nb.ipynb", "modify"),
    ]
    assert s.edits()[2].is_error is True
    interrupted, blocked, big = s.commands()
    assert interrupted.exit_code is None
    assert (blocked.exit_code, blocked.is_error) == (None, True)
    assert big.exit_code == 0
    assert len(big.output) <= OUTPUT_LIMIT
    assert big.output.startswith("HEAD") and big.output.endswith("TAIL")


# --- Codex -------------------------------------------------------------------------------


def test_codex_old_rollout() -> None:
    s = load_session(FIX / "codex_old.jsonl")
    assert (s.id, s.cwd, s.model) == (
        "0198aaaa-1111-7000-8000-000000000001",
        "/home/dev/demo",
        "gpt-test-5",
    )
    assert s.started_at == "2025-08-01T09:00:00.000Z"
    users = [e.text for e in s.of_kind(EventKind.USER)]
    assert users == ["Rename util.py to helpers.py and run the tests."]  # injected context skipped
    assert [(c.text, c.exit_code) for c in s.commands()] == [
        ("ls -la", 0),
        ("pytest -q", 1),
        ("pytest -q -x", 0),
    ]
    assert s.commands()[0].output == "util.py\n"
    assert s.commands()[2].output == "3 passed"
    assert [(e.action, e.path) for e in s.edits()] == [
        ("delete", "util.py"),
        ("add", "helpers.py"),
        ("add", "tests/test_helpers.py"),
        ("delete", "old.txt"),
    ]
    assert [(e.tool, e.text) for e in s.of_kind(EventKind.TOOL)] == [("view_image", "shot.png")]
    assert [e.text for e in s.assistant_messages()] == ["Renamed and the tests pass."]


def test_codex_code_mode() -> None:
    s = load_session(FIX / "codex_codemode.jsonl")
    assert kinds(s)[:2] == ["user", "assistant"]
    assert [(c.text, c.exit_code) for c in s.commands()] == [
        ('rg -n "--flag" src', 0),
        ("git status --short", None),  # two commands in one cell: exit codes are ambiguous
        ("uv run pytest -q", None),
        ("uv run pytest -q", 0),  # {cmd} shorthand resolved from a const
        ("uv run pytest -q", 0),  # quoted keys
    ]
    assert [(e.action, e.path) for e in s.edits()] == [
        ("add", "src/flags.py"),
        ("modify", "src/cli.py"),
    ]
    tools = [(e.tool, e.text) for e in s.of_kind(EventKind.TOOL)]
    assert tools[0][0] == "web__run"
    assert tools[1] == ("exec", "const x = [1, 2, 3].map(n => n * 2); text(x);")
    assert s.final_message() is not None


def test_codex_items_are_authoritative() -> None:
    s = load_session(FIX / "codex_items.jsonl")
    (cmd,) = s.commands()
    assert (cmd.text, cmd.exit_code, cmd.is_error, cmd.output) == (
        "uv run pytest -q",
        1,
        True,
        "1 failed",
    )
    assert cmd.timestamp == "2026-03-04T08:00:03.000Z"
    assert [(e.action, e.path) for e in s.edits()] == [
        ("modify", "C:\\work\\demo\\src\\a.py"),
        ("add", "C:\\work\\demo\\src\\b.py"),
        ("delete", "C:\\work\\demo\\src\\c.py"),
        ("add", "C:\\work\\demo\\src\\d.py"),
    ]
    assert [e.tool for e in s.of_kind(EventKind.TOOL)] == ["view_image"]


def test_codex_exec_json() -> None:
    s = load_session(FIX / "codex_exec.jsonl")
    assert s.id == "019c0000-0000-7000-8000-0000000e7ec0"
    assert kinds(s) == ["command", "edit", "edit", "tool", "tool", "assistant"]
    cmd = s.commands()[0]
    assert (cmd.text, cmd.exit_code, cmd.output) == ("pytest -q", 0, "2 passed")
    assert [(e.action, e.path) for e in s.edits()] == [("modify", "slug.py"), ("add", "new.py")]
    assert [e.tool for e in s.of_kind(EventKind.TOOL)] == ["docs.search", "web_search"]


def _rollout(path: Path, meta: dict[str, object], *records: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": meta["timestamp"], "type": "session_meta", "payload": meta}, *records]
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")


def _shell_call(ts: str, call_id: str, cmd: str) -> dict[str, object]:
    args = json.dumps({"cmd": cmd})
    payload = {
        "type": "function_call",
        "name": "exec_command",
        "arguments": args,
        "call_id": call_id,
    }
    return {"timestamp": ts, "type": "response_item", "payload": payload}


def test_codex_merges_spawned_subagents(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2026" / "05" / "06"
    parent_id = "019d0000-0000-7000-8000-0000000000aa"
    child_id = "019d0000-0000-7000-8000-0000000000bb"
    parent = day / f"rollout-2026-05-06T10-00-00-{parent_id}.jsonl"
    _rollout(
        parent,
        {"id": parent_id, "timestamp": "2026-05-06T10:00:00Z", "cwd": "/w"},
        _shell_call("2026-05-06T10:00:01Z", "p1", "ls"),
        _shell_call("2026-05-06T10:00:09Z", "p2", "pytest"),
    )
    spawn = {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}}
    _rollout(
        day / f"rollout-2026-05-06T10-00-02-{child_id}.jsonl",
        {
            "id": child_id,
            "timestamp": "2026-05-06T10:00:02Z",
            "cwd": "/w",
            "source": spawn,
            "agent_nickname": "Scout",
        },
        _shell_call("2026-05-06T10:00:05Z", "c1", "rg foo"),
    )
    guardian = {"subagent": {"other": "guardian"}}
    _rollout(
        day / "rollout-2026-05-06T10-00-03-019d0000-0000-7000-8000-0000000000cc.jsonl",
        {
            "id": "g",
            "timestamp": "2026-05-06T10:00:03Z",
            "source": guardian,
            "parent_thread_id": parent_id,
        },
        _shell_call("2026-05-06T10:00:06Z", "g1", "echo review"),
    )
    s = load_session(parent)
    assert [(c.text, c.actor) for c in s.commands()] == [
        ("ls", "main"),
        ("rg foo", "Scout"),
        ("pytest", "main"),
    ]
    assert len(load_session(parent, include_subagents=False).commands()) == 2


# --- Gemini CLI and generic --------------------------------------------------------------


def test_gemini_session() -> None:
    s = load_session(FIX / "gemini_session.json")
    assert (s.id, s.started_at, s.model) == (
        "9f8e7d6c-0000-4000-8000-000000000001",
        "2026-02-03T12:00:00.000Z",
        "gemini-test-pro",
    )
    assert kinds(s) == [
        "user",
        "assistant",
        "edit",
        "edit",
        "command",
        "command",
        "tool",
        "assistant",
    ]
    write, replace = s.edits()
    assert (write.path, write.action, write.is_error) == (
        "/home/dev/demo/hello.py",
        "unknown",
        False,
    )
    assert (replace.action, replace.is_error) == ("modify", True)
    assert [(c.text, c.exit_code) for c in s.commands()] == [
        ("python hello.py", 0),
        ("pytest -q", 1),
    ]
    tool = s.of_kind(EventKind.TOOL)[0]
    assert (tool.tool, tool.text, tool.output) == ("google_web_search", "python hello", "3 results")
    assert s.events[-1].text == "hello.py prints hi."


def test_gemini_jsonl_variant(tmp_path: Path) -> None:
    path = tmp_path / "session-1.jsonl"
    lines = [
        {"sessionId": "g-1", "projectHash": "h", "startTime": "2026-01-01T00:00:00Z"},
        {"id": "1", "type": "user", "content": [{"text": "hi"}]},
        {"id": "2", "type": "gemini", "content": "hello", "toolCalls": "not-a-list"},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    assert detect_agent(path) == "gemini-cli"
    s = load_session(path)
    assert s.id == "g-1"
    assert kinds(s) == ["user", "assistant"]
    assert s.warnings  # the malformed toolCalls is reported, not fatal


def test_generic_format() -> None:
    s = load_session(FIX / "generic.jsonl")
    assert (s.agent, s.id, s.cwd, s.model) == (
        "my-agent",
        "gen-001",
        "/home/dev/demo",
        "local-model",
    )
    assert kinds(s) == ["user", "command", "edit", "edit", "tool", "command", "assistant"]
    assert_indexed(s)
    assert s.commands()[0].exit_code == 0
    assert (s.commands()[1].exit_code, s.commands()[1].actor) == (None, "helper")
    assert [e.action for e in s.edits()] == ["modify", "unknown"]
    joined = "\n".join(s.warnings)
    assert "unknown edit action 'rename'" in joined
    assert "exit_code should be an integer" in joined
    assert "unknown kind 'mystery'" in joined


# --- errors ------------------------------------------------------------------------------


def test_missing_and_unrecognised_files(tmp_path: Path) -> None:
    with pytest.raises(TranscriptError, match="not found"):
        load_session(tmp_path / "nope.jsonl")
    with pytest.raises(TranscriptError, match="not a file"):
        load_session(tmp_path)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(TranscriptError, match="not a recognised transcript"):
        load_session(empty)
    junk = tmp_path / "junk.jsonl"
    junk.write_text('not json\n{"hello": 1}\n[1, 2]\n', encoding="utf-8")
    with pytest.raises(TranscriptError, match="not a recognised transcript"):
        detect_agent(junk)


def test_forced_agent_must_match(tmp_path: Path) -> None:
    with pytest.raises(TranscriptError, match="no Codex records"):
        load_session(CLAUDE, agent="codex")
    with pytest.raises(TranscriptError, match="unknown agent"):
        load_session(CLAUDE, agent="cursor")


def test_truncated_last_line_is_a_warning(tmp_path: Path) -> None:
    text = (FIX / "codex_old.jsonl").read_text(encoding="utf-8")
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        text + '{"timestamp":"2025-08-01T09:00:16Z","type":"response_it', encoding="utf-8"
    )
    s = load_session(path)
    assert len(s.commands()) == 3
    assert any("invalid JSON" in w for w in s.warnings)


def test_malformed_records_do_not_crash(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(
        _claude_lines(
            {
                "type": "assistant",
                "sessionId": "s",
                "message": {"content": [None, 5, {"type": "tool_use"}]},
            },
            {"type": "user", "sessionId": "s", "message": {"content": [{"type": "tool_result"}]}},
            {"type": "assistant", "sessionId": "s", "message": "oops"},
            {"type": "user", "sessionId": "s", "message": {"content": 42}},
        ),
        encoding="utf-8",
    )
    s = load_session(path)
    assert_indexed(s)
    assert [e.kind for e in s.events] == [EventKind.TOOL]
