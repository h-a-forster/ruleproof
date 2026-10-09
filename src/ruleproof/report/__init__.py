"""Reporters: render a ``Report`` as text, JSON, Markdown or SARIF.

Each reporter module exposes ``render(report, color) -> str``; ``render`` here dispatches by
format name. The helpers below are shared by the reporters.
"""

from __future__ import annotations

import re
from typing import Literal

from ruleproof.errors import ConfigError
from ruleproof.models import Evidence, Report, RuleResult, Status

Format = Literal["text", "json", "markdown", "sarif"]
FORMATS: tuple[Format, ...] = ("text", "json", "markdown", "sarif")

PROJECT_URL = "https://github.com/ruleproof/ruleproof"

STATUS_ORDER: tuple[Status, ...] = ("fail", "unverified", "skip", "pass")

_CONTROL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPACE = re.compile(r"\s+")


def render(
    report: Report,
    fmt: str,
    color: bool = False,
    *,
    quiet: bool = False,
    unicode: bool = True,
) -> str:
    """Render ``report`` in format ``fmt``. ``quiet`` and ``unicode`` only affect text."""
    if fmt == "text":
        from ruleproof.report import text

        return text.render(report, color, quiet=quiet, unicode=unicode)
    if fmt == "json":
        from ruleproof.report import json as json_report

        return json_report.render(report, color)
    if fmt == "markdown":
        from ruleproof.report import markdown

        return markdown.render(report, color)
    if fmt == "sarif":
        from ruleproof.report import sarif

        return sarif.render(report, color)
    raise ConfigError(f"unknown report format {fmt!r} (choose from {', '.join(FORMATS)})")


def by_status(report: Report) -> dict[Status, list[RuleResult]]:
    """Results grouped by status, in ``STATUS_ORDER``, keeping report order within a group."""
    groups: dict[Status, list[RuleResult]] = {s: [] for s in STATUS_ORDER}
    for r in report.results:
        groups[r.status].append(r)
    return groups


def counts(report: Report) -> dict[str, int]:
    groups = by_status(report)
    return {
        "total": len(report.results),
        "failed": len(groups["fail"]),
        "unverified": len(groups["unverified"]),
        "skipped": len(groups["skip"]),
        "passed": len(groups["pass"]),
    }


def tally(report: Report) -> str:
    """``"5 rules: 1 failed, 0 unverified, 2 skipped, 2 passed"``."""
    c = counts(report)
    noun = "rule" if c["total"] == 1 else "rules"
    return (
        f"{c['total']} {noun}: {c['failed']} failed, {c['unverified']} unverified, "
        f"{c['skipped']} skipped, {c['passed']} passed"
    )


def one_line(text: str, limit: int = 160) -> str:
    """Collapse whitespace, drop control characters and ANSI escapes, and truncate."""
    s = _SPACE.sub(" ", _CONTROL.sub("", text)).strip()
    if len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def location(ev: Evidence) -> str | None:
    """``path:line``, ``path`` or ``#event`` for a piece of evidence, if it has one."""
    if ev.path:
        return f"{ev.path}:{ev.line}" if ev.line else ev.path
    if ev.event is not None:
        return f"#{ev.event}"
    return None


def evidence_line(ev: Evidence, limit: int = 160) -> str:
    """One readable line: location, then the excerpt (or the message when there is none)."""
    loc = location(ev)
    message = one_line(ev.message, limit)
    excerpt = one_line(ev.excerpt, limit) if ev.excerpt else ""
    if excerpt and message and message != excerpt:
        body = f"{message}: {excerpt}"
    else:
        body = excerpt or message
    return f"{loc}  {body}" if loc else body


def split_source(source: str | None) -> tuple[str, int | None] | None:
    """``"AGENTS.md:12"`` -> ``("AGENTS.md", 12)``; ``"AGENTS.md"`` -> ``("AGENTS.md", None)``."""
    if not source:
        return None
    path, sep, line = source.rpartition(":")
    if sep and path and line.isdigit() and int(line) > 0:
        return path, int(line)
    return source, None
