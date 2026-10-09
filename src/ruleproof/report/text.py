"""Human-readable terminal report, grouped by status with failures first."""

from __future__ import annotations

from ruleproof.models import Report, RuleResult, Status
from ruleproof.report import by_status, evidence_line, one_line, tally

_SYMBOLS: dict[Status, str] = {
    "fail": "✗",
    "unverified": "?",
    "skip": "–",
    "pass": "✓",
}
_ASCII_SYMBOLS: dict[Status, str] = {"fail": "x", "unverified": "?", "skip": "-", "pass": "+"}
SYMBOL_CHARS = "".join(_SYMBOLS.values())
"""Characters the text report needs; the CLI falls back to ASCII when stdout can't encode them."""

_COLORS: dict[Status, str] = {"fail": "31", "unverified": "33", "skip": "2", "pass": "32"}
_BOLD = "1"
_DIM = "2"
_MAX_EVIDENCE = 10


class _Painter:
    def __init__(self, color: bool, unicode: bool) -> None:
        self.color = color
        self.symbols = _SYMBOLS if unicode else _ASCII_SYMBOLS

    def __call__(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.color else s

    def symbol(self, status: Status) -> str:
        return self(_COLORS[status], self.symbols[status])

    def head(self, r: RuleResult, summary: str) -> str:
        sev = "" if r.rule.severity == "error" else f" [{r.rule.severity}]"
        return f"{self.symbol(r.status)} {self(_BOLD, r.rule.id)}{sev}  {summary}"


def render(report: Report, color: bool, *, quiet: bool = False, unicode: bool = True) -> str:
    p = _Painter(color, unicode)
    groups = by_status(report)
    lines: list[str] = []

    for r in groups["fail"] + groups["unverified"]:
        lines.append(p.head(r, one_line(r.summary, 200)))
        lines.extend("    " + evidence_line(ev) for ev in r.evidence[:_MAX_EVIDENCE])
        if len(r.evidence) > _MAX_EVIDENCE:
            lines.append(f"    ... and {len(r.evidence) - _MAX_EVIDENCE} more")
        rule_line = _rule_line(r)
        if rule_line:
            lines.append("    " + p(_DIM, rule_line))

    for r in groups["skip"]:
        lines.append(p.head(r, p(_DIM, "skipped: " + one_line(r.summary, 200))))

    passed = groups["pass"]
    if passed and quiet:
        noun = "rule" if len(passed) == 1 else "rules"
        lines.append(f"{p.symbol('pass')} {len(passed)} {noun} passed")
    elif passed:
        lines.extend(p.head(r, one_line(r.summary, 200)) for r in passed)

    if lines:
        lines.append("")
    verdict: Status = "fail" if groups["fail"] else "unverified" if groups["unverified"] else "pass"
    lines.append(p(_COLORS[verdict], tally(report)))
    lines.extend(f"note: {one_line(n, 400)}" for n in report.notes)
    return "\n".join(lines) + "\n"


def _rule_line(r: RuleResult) -> str:
    desc = one_line(r.rule.description, 200)
    if r.rule.check == "doctor":
        return f"check: {desc}" if desc else ""
    where = r.rule.source or r.rule.origin
    if desc and where:
        return f"rule: {desc} ({where})"
    if desc:
        return f"rule: {desc}"
    return f"rule: defined at {where}" if where else ""
