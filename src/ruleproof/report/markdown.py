"""GitHub-flavoured Markdown report for PR comments and ``$GITHUB_STEP_SUMMARY``.

Everything that comes from rules, diffs or transcripts is untrusted: it is HTML-escaped in
table cells and ``<summary>`` tags and placed in fenced code blocks otherwise, so it cannot
inject markup into the comment.
"""

from __future__ import annotations

import html
import re

from ruleproof.models import Report, RuleResult, Status
from ruleproof.report import by_status, evidence_line, failed_label, one_line, tally

_MAX_EVIDENCE = 20
_COLLAPSED: tuple[tuple[Status, str], ...] = (("skip", "skipped"), ("pass", "passed"))


def render(report: Report, color: bool = False, *, fail_on: str | None = None) -> str:
    groups = by_status(report)
    problems = groups["fail"] + groups["unverified"]
    title = "ruleproof doctor" if report.kind == "doctor" else "ruleproof"
    if groups["fail"]:
        verdict = failed_label(report, fail_on)
    elif groups["unverified"]:
        verdict = f"{len(groups['unverified'])} unverified"
    else:
        verdict = "all rules passed" if report.results else "no rules ran"

    out: list[str] = [f"### {title}: {verdict}", "", tally(report, fail_on), ""]

    if problems:
        out += [
            "| Status | Rule | Severity | Summary | Source |",
            "| --- | --- | --- | --- | --- |",
        ]
        for r in problems:
            cells = [
                "fail" if r.status == "fail" else "unverified",
                code_span(r.rule.id, table=True),
                r.rule.severity,
                cell(one_line(r.summary, 200)),
                code_span(r.rule.source, table=True) if r.rule.source else "",
            ]
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
        for r in problems:
            out += _details(r)

    for status, label in _COLLAPSED:
        group = groups[status]
        if not group:
            continue
        out.append(f"<details><summary>{len(group)} {label}</summary>")
        out.append("")
        out += [f"- {code_span(r.rule.id)} {inline(one_line(r.summary, 200))}" for r in group]
        out += ["", "</details>", ""]

    if report.notes:
        out += [f"> {inline(one_line(n, 400))}" for n in report.notes]
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _details(r: RuleResult) -> list[str]:
    summary = html.escape(one_line(r.summary, 200))
    out = [f"<details><summary><code>{html.escape(r.rule.id)}</code>: {summary}</summary>", ""]
    if r.rule.description or r.rule.source:
        rule = inline(one_line(r.rule.description, 300)) if r.rule.description else "(rule)"
        where = f" ({code_span(r.rule.source)})" if r.rule.source else ""
        out += [f"Rule: {rule}{where}", ""]
    lines = [evidence_line(ev, 300) for ev in r.evidence[:_MAX_EVIDENCE]]
    if len(r.evidence) > _MAX_EVIDENCE:
        lines.append(f"... and {len(r.evidence) - _MAX_EVIDENCE} more")
    if lines:
        out += fenced("\n".join(lines))
        out.append("")
    out += ["</details>", ""]
    return out


def cell(text: str) -> str:
    """Escape text for a table cell: HTML-escaped, pipes escaped, one line."""
    return inline(text).replace("|", "\\|")


def inline(text: str) -> str:
    """Escape text for inline Markdown so it renders literally."""
    s = html.escape(text.replace("\n", " "), quote=False)
    return re.sub(r"([\\`*_\[\]#~])", r"\\\1", s)


def code_span(text: str, *, table: bool = False) -> str:
    """Inline code that survives backticks in ``text``; ``table`` also escapes pipes."""
    s = text.replace("\n", " ")
    if table:
        s = s.replace("|", "\\|")
    longest = max((len(m) for m in re.findall(r"`+", s)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{fence}{pad}{s}{pad}{fence}"


def fenced(text: str, lang: str = "text") -> list[str]:
    longest = max((len(m) for m in re.findall(r"`{3,}", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{lang}", text, fence]
