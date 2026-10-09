"""``ruleproof doctor``: lint agent instruction and config files.

Finds problems that make agents behave inconsistently: instruction files for different agents
that drifted apart, contradictions, instructions the repo disagrees with, dead references,
token bloat, broken skills and MCP config drift. Every finding is a failed ``RuleResult``
whose synthetic rule id is ``doctor/<check>``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ruleproof.doctor._common import DOCTOR_CHECKS, Finding, RepoInfo
from ruleproof.doctor.conflict import check_conflicts
from ruleproof.doctor.drift import check_drift
from ruleproof.doctor.mcp import check_mcp
from ruleproof.doctor.mismatch import check_mismatch
from ruleproof.doctor.references import check_references
from ruleproof.doctor.scan import build_scan
from ruleproof.doctor.size import check_size
from ruleproof.doctor.skills import check_skills
from ruleproof.errors import ConfigError
from ruleproof.models import RuleResult

__all__ = ["DOCTOR_CHECKS", "run_doctor"]


def run_doctor(
    repo: Path, *, max_tokens: int = 2500, exclude: Sequence[str] = ()
) -> list[RuleResult]:
    """Run every doctor check on ``repo``; results are sorted by severity, path and line.

    ``exclude`` holds repo-relative globs (``paths`` syntax) for files the doctor ignores.
    """
    if not repo.is_dir():
        raise ConfigError(f"not a directory: {repo}")
    info = RepoInfo(repo.resolve(), exclude)
    scan = build_scan(info)
    findings: list[Finding] = [
        *check_drift(info, scan),
        *check_conflicts(info, scan),
        *check_mismatch(info, scan),
        *check_references(info, scan),
        *check_size(info, max_tokens),
        *check_skills(info),
        *check_mcp(info),
    ]
    findings.sort(key=Finding.sort_key)
    return [f.result() for f in findings]
