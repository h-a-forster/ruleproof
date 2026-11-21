"""Incremental reads of a growing Claude Code transcript, for the hooks.

A hook runs on every tool call, and a full read costs time in proportion to the transcript.
``load`` keeps, per transcript, the parse state up to a byte offset in the user's cache
directory. The next call parses only the bytes appended since, through the same per-record
code as the full read (``claude.feed``), so the session it returns equals ``claude.parse``.

- Only whole lines are committed to the cache. An unterminated last line is parsed for the
  current result (as a full read would) and read again next time.
- The cached offset is trusted only while the file is at least that long and the bytes at its
  start and just before the offset still hash the same; otherwise (truncated or rewritten
  file) the transcript is read again from the start.
- A missing, corrupt, foreign or unwritable cache costs time, never correctness: the reader
  then starts from byte 0. Writes go to a temporary file that replaces the cache in one step,
  so concurrent hook processes see a whole file or none.
- With a ``deadline``, a read that runs out of time saves what it parsed and raises
  ``BudgetExceeded``; the next call continues from there.

The cache holds what the transcript holds (messages, commands, truncated output), so it lives
beside other per-user caches, never in the repo being checked.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, get_args

from ruleproof import __version__
from ruleproof.errors import TranscriptError
from ruleproof.models import EditAction, Event, EventKind, Session
from ruleproof.transcripts import claude
from ruleproof.transcripts._util import (
    finish,
    is_blank,
    maybe_whole_file_json,
    merge_streams,
    parse_line,
)

FORMAT = 1
"""Bump when the cached state or the parser's reading of a record changes."""

CACHE_DIR_ENV = "RULEPROOF_CACHE_DIR"
_CHUNK = 1 << 20
_FINGERPRINT_BYTES = 4096
_NEWLINE_RE = re.compile(rb"\r\n|\r|\n")  # the line ends text mode splits on
_STALE_DAYS = 30
_OWN_FILE_RE = re.compile(r"[0-9a-f]{32}(?:\.json|[a-z0-9_]{1,16}\.tmp)\Z")
_TMP_SUFFIX = ".tmp"

_ACTIONS: dict[str, EditAction] = {a: a for a in get_args(EditAction)}

Clock = Callable[[], float]


class BudgetExceeded(TranscriptError):
    """The read ran past its deadline; ``done`` of ``total`` bytes are parsed and cached."""

    def __init__(self, path: Path, done: int, total: int, saved: bool) -> None:
        self.done, self.total, self.saved = done, total, saved
        kept = "progress is saved" if saved else "progress could not be saved"
        super().__init__(
            f"{path}: read {done / 1e6:.0f} of {total / 1e6:.0f} MB before the time budget "
            f"ran out ({kept})"
        )


class _NotJsonl(Exception):
    """The file may hold one JSON document instead of JSONL: leave it to the full read."""


class _OutOfTime(Exception):
    pass


@dataclass(slots=True)
class _Stream:
    """One transcript file (the main one or a subagent's) and its parse state so far."""

    file: Path
    state: claude._State
    offset: int = 0  # bytes committed: everything before it is parsed, and ends a line
    lineno: int = 0
    started: bool = False  # a non-blank line was seen, and it was JSONL
    head: str = ""
    tail: str = ""
    warnings: list[str] = field(default_factory=list)
    dirty: bool = False

    @property
    def primary(self) -> bool:
        return self.state.actor == "main"


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where transcript caches live: a ``transcripts`` folder under ``RULEPROOF_CACHE_DIR``,
    else under the platform's user cache. Always a folder of its own, since old caches in it
    are deleted."""
    env = os.environ if env is None else env
    if env.get(CACHE_DIR_ENV):
        return Path(env[CACHE_DIR_ENV]).expanduser() / "transcripts"
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(env.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "ruleproof" / "transcripts"


def cache_file(path: Path, cache_dir: Path) -> Path:
    key = hashlib.sha256(str(_absolute(path)).encode("utf-8", "surrogatepass")).hexdigest()
    return cache_dir / f"{key[:32]}.json"


def load(
    path: Path,
    cache_dir: Path | None,
    *,
    include_subagents: bool = True,
    deadline: float | None = None,
    clock: Clock = time.monotonic,
) -> Session:
    """The session ``claude.parse(path)`` returns, reading only what the cache lacks.

    ``cache_dir=None`` reads everything and stores nothing. ``deadline`` is a ``clock()``
    value; past it the read stops with ``BudgetExceeded``.
    """
    if not path.is_file():
        raise TranscriptError(f"transcript not found: {path}")
    store = cache_file(path, cache_dir) if cache_dir is not None else None
    cached = _read_cache(store, path) if store is not None else {}
    session = Session(agent=claude.AGENT, id=path.stem, path=str(path))
    files = [path]
    if include_subagents:
        sub_dir = path.with_suffix("") / "subagents"
        if sub_dir.is_dir():
            files += sorted(sub_dir.rglob("agent-*.jsonl"))
    streams = [
        _restore(cached.get(str(f)), f, session, "main" if i == 0 else _sub_actor(f))
        for i, f in enumerate(files)
    ]
    fresh = not cached
    if deadline is not None and cached and clock() > deadline:
        # Loading the cache alone used up the budget: a retry would do the same.
        raise BudgetExceeded(path, 0, sum(_size(f) for f in files), saved=False)
    try:
        tails = [_advance(stream, deadline, clock) for stream in streams]
        if store is not None and (any(s.dirty for s in streams) or len(cached) != len(streams)):
            _write_cache(store, path, streams, fresh)
        for stream, tail in zip(streams, tails, strict=True):
            _feed_lines(stream, tail)  # not cached: the line may still be growing
    except _NotJsonl:
        return claude.parse(path, include_subagents)
    except _OutOfTime:
        saved = store is not None and _write_cache(store, path, streams, fresh)
        done = sum(s.offset for s in streams)
        total = sum(_size(f) for f in files)
        raise BudgetExceeded(path, done, total, saved) from None
    main = streams[0].state
    if main.recognised == 0:
        raise TranscriptError(f"{path}: no Claude Code records found")
    for stream in streams:
        claude.add_warnings(session, stream.file, stream.warnings, stream.primary)
    events = [s.state.events for s in streams]
    session.events = merge_streams(events) if len(events) > 1 else main.events
    return finish(session)


def _sub_actor(file: Path) -> str:
    return file.stem.removeprefix("agent-")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# --------------------------------------------------------------------------- reading


def _advance(stream: _Stream, deadline: float | None, clock: Clock) -> bytes:
    """Parse the whole lines after ``stream.offset``; return the unterminated rest."""
    with _open_binary(stream.file) as fh:
        if stream.offset and not _still_valid(stream, fh):
            _reset(stream)
        end = os.fstat(fh.fileno()).st_size
        fh.seek(stream.offset)
        parts: list[bytes] = []
        while chunk := fh.read(_CHUNK):
            parts.append(chunk)
            if b"\n" not in chunk:
                continue  # a line longer than the chunk
            block = b"".join(parts)
            cut = block.rindex(b"\n") + 1
            parts = [block[cut:]] if cut < len(block) else []
            _feed_lines(stream, block[:cut])
            stream.offset += cut
            stream.dirty = True
            if deadline is not None and fh.tell() < end and clock() > deadline:
                _fingerprint(stream, fh)
                raise _OutOfTime
        if stream.dirty:
            _fingerprint(stream, fh)
    return b"".join(parts)


def _open_binary(path: Path) -> BinaryIO:
    try:
        return open(path, "rb")
    except OSError as exc:
        raise TranscriptError(f"cannot read transcript {path}: {exc.strerror or exc}") from exc


def _feed_lines(stream: _Stream, block: bytes) -> None:
    # Text mode, which the full read uses, also ends lines at a lone \r. JSON escapes it
    # inside strings, so it is rare, and splitting on \n alone is much faster.
    lines = _NEWLINE_RE.split(block) if b"\r" in block else block.split(b"\n")
    if lines and not lines[-1]:
        lines.pop()  # the block ended with a line end
    for raw in lines:
        stream.lineno += 1
        line = raw.decode("utf-8", "replace")
        if not stream.started:
            if is_blank(line):
                continue
            if maybe_whole_file_json(line):
                raise _NotJsonl
            stream.started = True
        rec = parse_line(line, stream.lineno, stream.warnings)
        if rec is not None:
            claude.feed(stream.state, rec, stream.warnings, stream.primary)


def _hash_range(fh: BinaryIO, start: int, length: int) -> str:
    fh.seek(start)
    return hashlib.sha256(fh.read(length)).hexdigest()


def _fingerprint(stream: _Stream, fh: BinaryIO) -> None:
    n = min(_FINGERPRINT_BYTES, stream.offset)
    stream.head = _hash_range(fh, 0, n)
    stream.tail = _hash_range(fh, stream.offset - n, n)


def _still_valid(stream: _Stream, fh: BinaryIO) -> bool:
    """False when the file is shorter than the cached offset or its bytes changed."""
    if os.fstat(fh.fileno()).st_size < stream.offset:
        return False
    n = min(_FINGERPRINT_BYTES, stream.offset)
    return (
        _hash_range(fh, 0, n) == stream.head
        and _hash_range(fh, stream.offset - n, n) == stream.tail
    )


def _reset(stream: _Stream) -> None:
    """Forget what was cached for this file. The main file also carries the session's details."""
    session = stream.state.session
    if stream.primary:
        session.id = Path(session.path).stem
        session.cwd = session.started_at = session.model = None
    stream.state = claude._State(session=session, actor=stream.state.actor)
    stream.offset = stream.lineno = 0
    stream.started = False
    stream.head = stream.tail = ""
    stream.warnings = []
    stream.dirty = True


# --------------------------------------------------------------------------- cache file


def _read_cache(store: Path, path: Path) -> dict[str, Any]:
    """The cached streams by file path; empty when there is no usable cache."""
    try:
        data = json.loads(store.read_bytes())
    except (OSError, ValueError):
        return {}
    if (
        not isinstance(data, dict)
        or data.get("format") != FORMAT
        or data.get("ruleproof") != __version__
        or data.get("path") != str(_absolute(path))
        or not isinstance(data.get("streams"), dict)
    ):
        return {}
    streams: dict[str, Any] = data["streams"]
    return streams


def _restore(raw: object, file: Path, session: Session, actor: str) -> _Stream:
    """The cached stream, or a new one when ``raw`` is missing or malformed."""
    stream = _Stream(file, claude._State(session=session, actor=actor))
    if not isinstance(raw, dict):
        return stream
    try:
        _decode(stream, raw)
    except (LookupError, TypeError, ValueError):
        _reset(stream)
        stream.dirty = False
    return stream


def _decode(stream: _Stream, raw: dict[str, Any]) -> None:
    st = stream.state
    stream.offset, stream.lineno = _int(raw["offset"]), _int(raw["lineno"])
    stream.head, stream.tail = _str(raw["head"]), _str(raw["tail"])
    stream.started = raw["started"] is True
    stream.warnings = [_str(w) for w in raw["warnings"]]
    st.recognised, st.got_id = _int(raw["recognised"]), raw["got_id"] is True
    st.seen_text = {_str(k) for k in raw["seen"]}
    for row in raw["events"]:
        kind, text, ts, tool, path, action, exit_code, output, is_error, ev_actor, inp = row
        ev = Event(0, EventKind(kind), _str(text), _opt(ts), _opt(tool), _opt(path))
        ev.action = None if action is None else _ACTIONS[action]
        ev.exit_code = None if exit_code is None else _int(exit_code)
        ev.output, ev.actor, ev.input = _str(output), _str(ev_actor), _str(inp)
        if is_error is not None and not isinstance(is_error, bool):
            raise TypeError("expected a boolean")
        ev.is_error = is_error
        st.events.append(ev)
    for tool_id, (index, background) in raw["pending"].items():
        st.pending[tool_id] = claude._Pending(st.events[_int(index)], background is True)
    if stream.primary:
        sid, cwd, started_at, model = raw["session"]
        session = st.session
        session.id, session.cwd = _str(sid), _opt(cwd)
        session.started_at, session.model = _opt(started_at), _opt(model)


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a non-negative integer")
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a string")
    return value


def _opt(value: object) -> str | None:
    return None if value is None else _str(value)


def _encode(stream: _Stream) -> dict[str, Any]:
    st = stream.state
    index = {id(ev): i for i, ev in enumerate(st.events)}
    raw: dict[str, Any] = {
        "offset": stream.offset,
        "lineno": stream.lineno,
        "head": stream.head,
        "tail": stream.tail,
        "started": stream.started,
        "warnings": stream.warnings,
        "recognised": st.recognised,
        "got_id": st.got_id,
        "seen": sorted(st.seen_text),
        "events": [
            [
                e.kind.value,
                e.text,
                e.timestamp,
                e.tool,
                e.path,
                e.action,
                e.exit_code,
                e.output,
                e.is_error,
                e.actor,
                e.input,
            ]
            for e in st.events
        ],
        "pending": {k: [index[id(p.event)], p.background] for k, p in st.pending.items()},
    }
    if stream.primary:
        s = st.session
        raw["session"] = [s.id, s.cwd, s.started_at, s.model]
    return raw


def _write_cache(store: Path, path: Path, streams: list[_Stream], prune: bool) -> bool:
    """Replace the cache in one step. False when it cannot be written; the caller's result
    does not depend on it."""
    data = {
        "format": FORMAT,
        "ruleproof": __version__,
        "path": str(_absolute(path)),
        "streams": {str(s.file): _encode(s) for s in streams},
    }
    tmp: str | None = None
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        if prune:
            _prune(store.parent)
        fd, tmp = tempfile.mkstemp(dir=store.parent, prefix=store.stem, suffix=_TMP_SUFFIX)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, separators=(",", ":")))
        os.replace(tmp, store)
    except (OSError, ValueError):
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False
    return True


def _prune(cache_dir: Path) -> None:
    """Drop caches of sessions nobody has touched for a month, and abandoned temporary files."""
    now = time.time()
    with os.scandir(cache_dir) as entries:
        for entry in entries:
            if not _OWN_FILE_RE.match(entry.name):
                continue  # never delete what ruleproof did not write
            days = 1 if entry.name.endswith(_TMP_SUFFIX) else _STALE_DAYS
            try:
                if now - entry.stat().st_mtime > days * 86400:
                    os.unlink(entry.path)
            except OSError:
                continue
