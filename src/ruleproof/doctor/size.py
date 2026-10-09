"""``doctor/size``: estimated instruction tokens each agent loads at the start of a session."""

from __future__ import annotations

import math

from ruleproof.doctor._common import Doc, Finding, RepoInfo, ev
from ruleproof.doctor.imports import claude_roots, codex_roots, gemini_roots, import_closure
from ruleproof.models import Severity

CODEX_DOC_LIMIT = 32 * 1024
"""Codex's default ``project_doc_max_bytes``; it truncates project docs beyond this."""


def tokens(chars: int) -> int:
    """Rough token estimate: about four characters per token."""
    return math.ceil(chars / 4)


def check_size(info: RepoInfo, max_tokens: int) -> list[Finding]:
    agents: list[tuple[str, list[Doc]]] = [
        ("Claude Code", import_closure(info, claude_roots(info, info.repo))),
        ("Codex", [d for p in codex_roots(info, info.repo) if (d := info.doc(p))]),
        ("Gemini CLI", import_closure(info, gemini_roots(info, info.repo))),
    ]
    out: list[Finding] = []
    for agent, docs in agents:
        if not docs:
            continue
        sizes = sorted(((tokens(len(d.text)), d) for d in docs), key=lambda t: -t[0])
        total = sum(t for t, _ in sizes)
        truncated = agent == "Codex" and sum(len(d.text.encode()) for d in docs) > CODEX_DOC_LIMIT
        if total <= max_tokens and not truncated:
            continue
        severity: Severity = "warning" if total > 2 * max_tokens or truncated else "info"
        top = ", ".join(f"{d.rel} {t:,}" for t, d in sizes[:3])
        summary = (
            f"{agent} loads about {total:,} tokens of instructions every session "
            f"(estimated as characters / 4; budget {max_tokens:,}): {top}"
        )
        if truncated:
            summary += "; Codex truncates project docs beyond 32 KiB by default"
        evidence = [ev(f"about {t:,} tokens", d.rel) for t, d in sizes[:5]]
        out.append(Finding("size", severity, summary, evidence))
    return out
