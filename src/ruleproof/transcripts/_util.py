"""Helpers shared by the transcript parsers: robust JSON reading, timestamps, summaries."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from ruleproof.errors import TranscriptError
from ruleproof.models import Event, Session

BOM = chr(0xFEFF)
MAX_WARNINGS = 50
_PEEK = 1 << 20
SUMMARY_LIMIT = 200

_SUMMARY_KEYS = (
    "command",
    "cmd",
    "file_path",
    "notebook_path",
    "path",
    "pattern",
    "url",
    "query",
    "description",
    "prompt",
    "message",
    "code",
)


def warn(warnings: list[str], message: str) -> None:
    """Append a parse warning, capping the list so a broken file cannot flood a report."""
    if len(warnings) < MAX_WARNINGS:
        warnings.append(message)
    elif len(warnings) == MAX_WARNINGS:
        warnings.append("further warnings suppressed")


def open_text(path: Path) -> TextIO:
    try:
        return open(path, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TranscriptError(f"cannot read transcript {path}: {exc.strerror or exc}") from exc


def iter_records(path: Path, warnings: list[str]) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects of a JSONL file, or of a file holding one JSON array/object.

    JSONL is streamed line by line. Malformed lines are skipped with a warning. CRLF and a
    leading byte-order mark are tolerated.
    """
    with open_text(path) as fh:
        head = fh.read(_PEEK).lstrip(BOM)
        start = head.lstrip()
        if start.startswith("[") or (start.startswith("{") and not _json_line(start)):
            try:
                whole = json.loads(start + fh.read())
            except json.JSONDecodeError:
                pass  # JSONL after all; fall through
            else:
                for i, item in enumerate(whole if isinstance(whole, list) else [whole]):
                    if isinstance(item, dict):
                        yield item
                    else:
                        warn(warnings, f"item {i}: expected a JSON object")
                return
        fh.seek(0)
        for lineno, line in enumerate(fh, 1):
            line = line.strip().lstrip(BOM)
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                warn(warnings, f"line {lineno}: invalid JSON ({exc.msg})")
                continue
            if isinstance(rec, dict):
                yield rec
            else:
                warn(warnings, f"line {lineno}: expected a JSON object")


def _json_line(text: str) -> bool:
    """True when the first line of ``text`` is a complete JSON object (JSONL, not pretty JSON)."""
    try:
        return isinstance(json.loads(text.partition("\n")[0]), dict)
    except json.JSONDecodeError:
        return False


def str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def parse_ts(value: str | None) -> float | None:
    """Seconds since the epoch for an ISO-8601 timestamp, or None if unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def ms_to_iso(ms: object) -> str | None:
    n = int_or_none(ms)
    if n is None:
        return None
    try:
        dt = datetime.fromtimestamp(n / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def one_line(text: str, limit: int = SUMMARY_LIMIT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def summarize(value: object) -> str:
    """Short one-line summary of a tool input (dict, JSON string, or anything else)."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return one_line(value)
        value = decoded
    if isinstance(value, dict):
        for key in _SUMMARY_KEYS:
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return one_line(v)
    if value is None or value == {}:
        return ""
    try:
        return one_line(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return one_line(str(value))


def content_text(content: object) -> str:
    """Join the text of a message/tool-result content: a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(part.get("content"), (str, list)):
                    parts.append(content_text(part["content"]))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return str(content["text"])
    return ""


def merge_streams(streams: list[list[Event]]) -> list[Event]:
    """Merge event lists by timestamp, keeping each list's own order.

    Events without a parseable timestamp sort with the previous event of their own stream.
    Ties go to the earlier stream (the main transcript first).
    """
    keyed: list[tuple[float, int, int, Event]] = []
    for s, events in enumerate(streams):
        last = float("-inf")
        for i, ev in enumerate(events):
            ts = parse_ts(ev.timestamp)
            if ts is not None and ts >= last:
                last = ts
            keyed.append((last, s, i, ev))
    keyed.sort(key=lambda k: (k[0], k[1], k[2]))
    return [k[3] for k in keyed]


def finish(session: Session) -> Session:
    for i, ev in enumerate(session.events):
        ev.index = i
    return session
