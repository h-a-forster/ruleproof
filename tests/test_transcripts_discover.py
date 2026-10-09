from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from ruleproof.errors import TranscriptError
from ruleproof.transcripts.discover import find_sessions, is_inside, resolve_session


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("claude", "codex", "gemini_home")}
    for p in paths.values():
        p.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(paths["claude"]))
    monkeypatch.setenv("CODEX_HOME", str(paths["codex"]))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(paths["gemini_home"]))
    paths["repo"] = (tmp_path / "work" / "demo").resolve()
    paths["repo"].mkdir(parents=True)
    return paths


def _touch(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def _claude(home: Path, cwd: str, sid: str, mtime: float) -> Path:
    d = home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    lines = [
        {"type": "queue-operation", "sessionId": sid, "timestamp": "2026-01-01T00:00:00Z"},
        {
            "type": "user",
            "cwd": cwd,
            "sessionId": sid,
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "user", "content": "hi"},
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    _touch(path, mtime)
    return path


def _codex(home: Path, sub: str, sid: str, cwd: str, mtime: float, source: object = "cli") -> Path:
    path = home / sub / f"rollout-2026-01-02T03-04-05-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"id": sid, "timestamp": "2026-01-02T03:04:05Z", "cwd": cwd, "source": source}
    path.write_text(
        json.dumps({"timestamp": "x", "type": "session_meta", "payload": meta}) + "\n",
        encoding="utf-8",
    )
    _touch(path, mtime)
    return path


def test_find_sessions_filters_by_repo_and_sorts_newest_first(homes: dict[str, Path]) -> None:
    repo = homes["repo"]
    inside = _claude(homes["claude"], str(repo), "aaaa1111-0000", 1000)
    sub = _claude(homes["claude"], str(repo / "pkg"), "aaaa2222-0000", 3000)
    _claude(homes["claude"], str(repo) + "-old", "bbbb0000-0000", 4000)  # sibling dir: excluded
    _claude(homes["claude"], str(repo.parent / "other"), "cccc0000-0000", 5000)
    cx = _codex(
        homes["codex"],
        "sessions/2026/01/02",
        "dddd0000-0000-7000-8000-000000000001",
        str(repo),
        2000,
    )
    archived = _codex(
        homes["codex"], "archived_sessions", "eeee0000-0000-7000-8000-000000000002", str(repo), 500
    )
    _codex(
        homes["codex"],
        "sessions/2026/01/02",
        "ffff0000-0000-7000-8000-000000000003",
        str(repo),
        6000,
        source={"subagent": {"thread_spawn": {"parent_thread_id": "x"}}},
    )

    refs = find_sessions(repo)
    assert [r.path for r in refs] == [sub, cx, inside, archived]
    assert [r.agent for r in refs] == ["claude-code", "codex", "claude-code", "codex"]
    assert refs[1].id == "dddd0000-0000-7000-8000-000000000001"
    assert refs[1].started_at == "2026-01-02T03:04:05Z"
    assert refs[0].cwd == str(repo / "pkg")

    assert [r.path for r in find_sessions(repo, limit=2)] == [sub, cx]
    assert [r.path for r in find_sessions(repo, agent="codex")] == [cx, archived]
    assert len(find_sessions(repo, all_projects=True)) == 6
    assert find_sessions(repo, agent="generic") == []


def test_find_sessions_case_and_msys_paths(homes: dict[str, Path]) -> None:
    assert is_inside("/c/Users/Me/Repo/src", "C:\\Users\\me\\repo")
    assert is_inside("C:\\Users\\me\\repo", "c:/users/ME/Repo/")
    assert not is_inside("C:\\Users\\me\\repo2", "C:\\Users\\me\\repo")
    if os.name != "nt":
        assert not is_inside("/home/Me/repo", "/home/me/repo")


def test_find_sessions_gemini(homes: dict[str, Path]) -> None:
    repo = homes["repo"]
    tmp = homes["gemini_home"] / ".gemini" / "tmp"
    by_hash = tmp / hashlib.sha256(str(repo).encode()).hexdigest() / "chats"
    by_hash.mkdir(parents=True)
    session = by_hash / "session-2026-01-01T00-00-abc.json"
    session.write_text(
        json.dumps({"sessionId": "gem-1", "startTime": "2026-01-01T00:00:00Z", "messages": []}),
        encoding="utf-8",
    )
    marked = tmp / "demo-project" / "chats"
    marked.mkdir(parents=True)
    (tmp / "demo-project" / ".project_root").write_text(str(repo / "web"), encoding="utf-8")
    (marked / "session-2.json").write_text(
        '{"sessionId": "gem-2", "messages": []}', encoding="utf-8"
    )
    other = tmp / ("0" * 64) / "chats"
    other.mkdir(parents=True)
    (other / "session-3.json").write_text(
        '{"sessionId": "gem-3", "messages": []}', encoding="utf-8"
    )

    refs = find_sessions(repo, agent="gemini-cli")
    assert sorted(r.id for r in refs) == ["gem-1", "gem-2"]
    assert {r.id: r.started_at for r in refs}["gem-1"] == "2026-01-01T00:00:00Z"


def test_resolve_session(homes: dict[str, Path], tmp_path: Path) -> None:
    repo = homes["repo"]
    a = _claude(homes["claude"], str(repo), "abc12345-0001", 1000)
    b = _claude(homes["claude"], str(repo), "abc12345-0002", 2000)
    far = _claude(homes["claude"], str(tmp_path / "elsewhere"), "fff99999-0001", 3000)

    assert resolve_session("latest", repo).path == b
    assert resolve_session("abc12345-0001", repo).path == a
    assert resolve_session("fff9", repo).path == far  # falls back to all projects
    ref = resolve_session(str(a), repo)
    assert (ref.agent, ref.id, ref.cwd) == ("claude-code", "abc12345-0001", str(repo))

    with pytest.raises(TranscriptError, match="ambiguous") as exc:
        resolve_session("abc1", repo)
    assert "abc12345-0001" in str(exc.value) and "abc12345-0002" in str(exc.value)
    with pytest.raises(TranscriptError, match="no session file or session id"):
        resolve_session("zzz", repo)
    with pytest.raises(TranscriptError, match="no codex sessions found"):
        resolve_session("latest", repo, agent="codex")
    with pytest.raises(TranscriptError, match="unknown agent"):
        find_sessions(repo, agent="cursor")


def test_resolve_generic_file(homes: dict[str, Path], tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"kind": "user", "text": "hi"}\n', encoding="utf-8")
    ref = resolve_session(str(path), homes["repo"])
    assert (ref.agent, ref.id, ref.path) == ("generic", "events", path)
