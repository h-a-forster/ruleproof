"""Find agent sessions on disk without parsing them.

Homes: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``; ``$CODEX_HOME`` or ``~/.codex``;
``$GEMINI_CLI_HOME`` or ``~``, holding ``.gemini``. Listing reads only the first lines of each
file (enough for the session id, working directory and start time).

A session belongs to a repo when the working directory it recorded at start is the repo or a
directory inside it. A session started in a parent folder that later ``cd``-ed into the repo is
therefore not found; pass its transcript path or id instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from ruleproof.errors import TranscriptError
from ruleproof.transcripts import AGENTS, detect_agent
from ruleproof.transcripts._util import BOM, open_text, str_or_none

DISCOVERABLE = ("claude-code", "codex", "gemini-cli")
_HEAD_LINES = 100
_MSYS_RE = re.compile(r"^[\\/]([A-Za-z])(?:[\\/](.*))?$")  # Path("/c/x") prints as \c\x
_CLAUDE_DIR_MAX = 200  # Claude Code shortens longer encoded project names


@dataclass(frozen=True, slots=True)
class SessionRef:
    agent: str
    id: str
    path: Path
    cwd: str | None
    started_at: str | None
    mtime: float


def claude_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def gemini_home() -> Path:
    env = os.environ.get("GEMINI_CLI_HOME")
    if not env:
        return Path.home() / ".gemini"
    base = Path(env).expanduser()
    return base / ".gemini" if (base / ".gemini").is_dir() else base


def find_sessions(
    repo: Path,
    agent: str | None = None,
    all_projects: bool = False,
    limit: int | None = None,
) -> list[SessionRef]:
    """Sessions whose working directory is ``repo`` or inside it, newest first."""
    if agent is not None and agent not in AGENTS:
        raise TranscriptError(f"unknown agent {agent!r}; expected one of {', '.join(AGENTS)}")
    agents = [agent] if agent else list(DISCOVERABLE)
    repo_s = str(Path(native_path(str(repo))).expanduser().resolve())
    candidates: list[tuple[float, str, Path, str | None]] = []
    if "claude-code" in agents:
        candidates += [
            (_mtime(p), "claude-code", p, None) for p in _claude_files(repo_s, all_projects)
        ]
    if "codex" in agents:
        candidates += [(_mtime(p), "codex", p, None) for p in _codex_files()]
    if "gemini-cli" in agents:
        candidates += [(_mtime(p), "gemini-cli", p, cwd) for p, cwd in _gemini_files(repo_s)]
    candidates.sort(key=lambda c: c[0], reverse=True)
    out: list[SessionRef] = []
    for mtime, name, path, cwd_hint in candidates:
        if limit is not None and len(out) >= limit:
            break
        ref = _head(name, path, mtime, cwd_hint)
        if ref is None:
            continue
        if all_projects or (ref.cwd is not None and is_inside(ref.cwd, repo_s)):
            out.append(ref)
    return out


def resolve_session(spec: str, repo: Path, agent: str | None = None) -> SessionRef:
    """``latest``, a transcript path, or a session-id prefix → one session."""
    if not spec.strip():
        raise TranscriptError("empty session id; pass latest, a transcript path or a session id")
    if spec == "latest":
        refs = find_sessions(repo, agent, limit=1)
        if not refs:
            who = agent or "agent"
            raise TranscriptError(
                f"no {who} sessions found for {repo}; pass --transcript FILE or --session ID"
            )
        return refs[0]
    path = Path(native_path(spec)).expanduser()
    if path.is_file():
        name = agent or detect_agent(path)
        ref = _head(name, path, _mtime(path), None) if name in DISCOVERABLE else None
        return ref or SessionRef(name, path.stem, path, None, None, _mtime(path))
    matches = _by_prefix(find_sessions(repo, agent), spec)
    if not matches:
        matches = _by_prefix(find_sessions(repo, agent, all_projects=True), spec)
    if not matches:
        raise TranscriptError(f"no session file or session id matching {spec!r}")
    exact = [r for r in matches if r.id.lower() == spec.lower()]
    if len(exact) == 1 or len(matches) == 1:
        return (exact or matches)[0]
    listing = "\n".join(
        f"  {r.id}  {r.agent}  {r.started_at or '?'}  {r.path}" for r in matches[:10]
    )
    more = f"\n  ... and {len(matches) - 10} more" if len(matches) > 10 else ""
    raise TranscriptError(f"session id prefix {spec!r} is ambiguous:\n{listing}{more}")


def _by_prefix(refs: list[SessionRef], spec: str) -> list[SessionRef]:
    s = spec.lower()
    return [r for r in refs if r.id.lower().startswith(s) or r.path.stem.lower().startswith(s)]


def native_path(path: str) -> str:
    """On Windows, ``/c/Users/x`` (Git Bash / MSYS) -> ``C:/Users/x``; elsewhere unchanged."""
    m = _MSYS_RE.match(path.strip())
    if os.name == "nt" and m:
        return f"{m.group(1).upper()}:/{m.group(2) or ''}"
    return path


def normalize_dir(path: str) -> str:
    """Comparable form of a directory: forward slashes, no trailing slash, MSYS drive paths
    converted, case-folded when it is a Windows path."""
    s = path.strip().replace("\\", "/")
    m = re.match(r"^/([A-Za-z])(?=/|$)", s)
    if m and (os.name == "nt" or not Path(s).exists()):
        s = f"{m.group(1)}:{s[2:] or '/'}"
    s = s.rstrip("/") or "/"
    if re.match(r"^[A-Za-z]:", s) or os.name == "nt":
        s = s.lower()
    return s


def is_inside(cwd: str, repo: str) -> bool:
    c, r = normalize_dir(cwd), normalize_dir(repo)
    return c == r or c.startswith(r.rstrip("/") + "/")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _claude_encodings(repo: str) -> set[str]:
    forms = {repo, repo.replace("\\", "/")}
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", repo)
    if m:
        forms.add(f"/{m.group(1).lower()}/{m.group(2)}".replace("\\", "/"))
    return {re.sub(r"[^A-Za-z0-9]", "-", f).lower() for f in forms}


def _claude_dir_matches(name: str, encodings: set[str]) -> bool:
    low = name.lower()
    for enc in encodings:
        if len(enc) >= _CLAUDE_DIR_MAX and low.startswith(enc[: _CLAUDE_DIR_MAX // 2]):
            return True
        if low == enc or low.startswith(enc + "-"):
            return True
    return False


def _claude_files(repo: str, all_projects: bool) -> Iterator[Path]:
    projects = claude_home() / "projects"
    if not projects.is_dir():
        return
    encodings = _claude_encodings(repo)
    for d in projects.iterdir():
        if d.is_dir() and (all_projects or _claude_dir_matches(d.name, encodings)):
            yield from (p for p in d.glob("*.jsonl") if p.is_file())


def _codex_files() -> Iterator[Path]:
    home = codex_home()
    for sub in ("sessions", "archived_sessions"):
        root = home / sub
        if root.is_dir():
            yield from (p for p in root.rglob("rollout-*.jsonl") if p.is_file())


def _gemini_files(repo: str) -> Iterator[tuple[Path, str | None]]:
    tmp = gemini_home() / "tmp"
    if not tmp.is_dir():
        return
    roots = _gemini_project_roots(repo)
    for d in tmp.iterdir():
        chats = d / "chats"
        if not chats.is_dir():
            continue
        cwd = roots.get(d.name) or _read_head(d / ".project_root", 4096).strip() or None
        for p in chats.iterdir():
            if p.name.startswith("session-") and p.suffix in (".json", ".jsonl"):
                yield p, cwd


def _gemini_project_roots(repo: str) -> dict[str, str]:
    """Map Gemini temp-dir names to project roots (registry file, then hashes of ``repo``)."""
    roots: dict[str, str] = {}
    registry = _read_json(gemini_home() / "projects.json")
    if isinstance(registry, dict):
        entries = registry.get("projects", registry)
        if isinstance(entries, dict):
            for root, value in entries.items():
                ident = value if isinstance(value, str) else None
                if isinstance(value, dict):
                    ident = value.get("id") or value.get("slug") or value.get("name")
                if isinstance(ident, str):
                    roots[ident] = root
    forms = {repo, repo.replace("\\", "/")}
    if re.match(r"^[A-Za-z]:", repo):
        forms |= {repo[0].lower() + repo[1:], repo[0].upper() + repo[1:]}
    for form in forms:
        roots.setdefault(hashlib.sha256(form.encode("utf-8")).hexdigest(), repo)
    return roots


def _read_head(path: Path, size: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(size)
    except OSError:
        return ""


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _head_lines(path: Path, max_lines: int = _HEAD_LINES) -> Iterator[dict[str, Any]]:
    """JSON objects among the first ``max_lines`` lines (whole lines, however long)."""
    try:
        with open_text(path) as fh:
            for line in islice(fh, max_lines):
                try:
                    rec = json.loads(line.lstrip(BOM))
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except (OSError, TranscriptError):
        return


def _head(agent: str, path: Path, mtime: float, cwd_hint: str | None) -> SessionRef | None:
    if agent == "claude-code":
        return _claude_head(path, mtime)
    if agent == "codex":
        return _codex_head(path, mtime)
    if agent == "gemini-cli":
        return _gemini_head(path, mtime, cwd_hint)
    return None


def _claude_head(path: Path, mtime: float) -> SessionRef | None:
    cwd = started = sid = None
    for rec in _head_lines(path):
        cwd = cwd or str_or_none(rec.get("cwd"))
        sid = sid or str_or_none(rec.get("sessionId")) or str_or_none(rec.get("session_id"))
        started = started or str_or_none(rec.get("timestamp"))
        if cwd and sid and started:
            break
    if cwd is None:
        return None
    return SessionRef("claude-code", sid or path.stem, path, cwd, started, mtime)


def _codex_head(path: Path, mtime: float) -> SessionRef | None:
    cwd = started = sid = None
    for n, rec in enumerate(_head_lines(path, 20)):
        payload = rec.get("payload")
        p: dict[str, Any] = payload if isinstance(payload, dict) else rec
        if n == 0 and rec.get("type") == "session_meta" and _spawned(p):
            return None
        sid = sid or (str_or_none(p.get("id")) if n == 0 else None)
        cwd = cwd or str_or_none(p.get("cwd"))
        started = started or str_or_none(p.get("timestamp")) or str_or_none(rec.get("timestamp"))
        if cwd:
            break
    if sid is None and cwd is None:
        return None
    m = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})$", path.stem, re.I)
    return SessionRef("codex", sid or (m.group(1) if m else path.stem), path, cwd, started, mtime)


def _spawned(payload: dict[str, Any]) -> bool:
    """True for subagent and review threads, which are listed under their parent."""
    source = payload.get("source")
    return bool(payload.get("parent_thread_id")) or (
        isinstance(source, dict) and "subagent" in source
    )


def _gemini_head(path: Path, mtime: float, cwd: str | None) -> SessionRef | None:
    head = _read_head(path, 8192)
    sid = re.search(r'"sessionId"\s*:\s*"([^"]+)"', head)
    started = re.search(r'"startTime"\s*:\s*"([^"]+)"', head)
    root = re.search(r'"(?:projectRoot|cwd)"\s*:\s*"((?:[^"\\]|\\.)*)"', head)
    if root and cwd is None:
        try:
            cwd = json.loads(f'"{root.group(1)}"')
        except json.JSONDecodeError:
            cwd = None
    return SessionRef(
        "gemini-cli",
        sid.group(1) if sid else path.stem,
        path,
        cwd,
        started.group(1) if started else None,
        mtime,
    )
