"""Machine-readable JSON report with a stable schema (``ruleproof/report@1``)."""

from __future__ import annotations

import json
from typing import Any

from ruleproof.models import Evidence, Report, RuleResult
from ruleproof.report import counts

SCHEMA = "ruleproof/report@1"


def render(report: Report, color: bool = False) -> str:
    return json.dumps(to_dict(report), indent=2, ensure_ascii=False) + "\n"


def to_dict(report: Report) -> dict[str, Any]:
    session = report.session
    return {
        "schema": SCHEMA,
        "kind": report.kind,
        "version": report.tool_version,
        "repo": report.repo,
        "base": report.base,
        "session": (
            {"agent": session.agent, "id": session.id, "path": session.path} if session else None
        ),
        "summary": counts(report),
        "results": [_result(r) for r in report.results],
        "notes": list(report.notes),
    }


def _result(r: RuleResult) -> dict[str, Any]:
    return {
        "id": r.rule.id,
        "check": r.rule.check,
        "status": r.status,
        "severity": r.rule.severity,
        "summary": r.summary,
        "description": r.rule.description,
        "source": r.rule.source,
        "origin": r.rule.origin,
        "evidence": [_evidence(e) for e in r.evidence],
    }


def _evidence(e: Evidence) -> dict[str, Any]:
    return {
        "message": e.message,
        "path": e.path,
        "line": e.line,
        "event": e.event,
        "excerpt": e.excerpt,
    }
