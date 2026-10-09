"""SARIF 2.1.0 report for GitHub code scanning and other SARIF consumers.

One ``reportingDescriptor`` per rule, one ``result`` per failing or unverified rule. A
result points at its first evidence location; when the evidence has no file location (a
transcript event) it points at the instruction line the rule came from, so the annotation
lands on the prose rule that was broken.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from ruleproof.models import Report, RuleResult, Severity
from ruleproof.report import PROJECT_URL, evidence_line, one_line, split_source

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

_LEVELS: dict[Severity, str] = {"error": "error", "warning": "warning", "info": "note"}
_MAX_EVIDENCE = 20


def render(report: Report, color: bool = False) -> str:
    return json.dumps(to_sarif(report), indent=2, ensure_ascii=False) + "\n"


def to_sarif(report: Report) -> dict[str, Any]:
    rule_index: dict[str, int] = {}
    rules: list[dict[str, Any]] = []
    for r in report.results:
        if r.rule.id not in rule_index:
            rule_index[r.rule.id] = len(rules)
            rules.append(_descriptor(r))

    results = [
        _result(r, rule_index[r.rule.id])
        for r in report.results
        if r.status in ("fail", "unverified")
    ]
    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "ruleproof",
                "version": report.tool_version,
                "semanticVersion": report.tool_version,
                "informationUri": PROJECT_URL,
                "rules": rules,
            }
        },
        "results": results,
    }
    if report.notes:
        run["properties"] = {"notes": list(report.notes)}
    return {"$schema": SARIF_SCHEMA, "version": SARIF_VERSION, "runs": [run]}


def _descriptor(r: RuleResult) -> dict[str, Any]:
    rule = r.rule
    short = one_line(rule.description, 200) or rule.id
    help_lines = [rule.description or rule.id]
    if rule.source:
        help_lines.append(f"Source: {rule.source}")
    help_lines.append(f"Check: {rule.check}")
    descriptor: dict[str, Any] = {
        "id": rule.id,
        "name": rule.id,
        "shortDescription": {"text": short},
        "help": {"text": "\n".join(help_lines)},
        "defaultConfiguration": {"level": _LEVELS[rule.severity]},
        "properties": {"check": rule.check},
    }
    if rule.source:
        descriptor["properties"]["source"] = rule.source
    return descriptor


def _result(r: RuleResult, index: int) -> dict[str, Any]:
    lines = [one_line(r.summary, 500) or r.rule.id]
    lines += [evidence_line(ev, 300) for ev in r.evidence[:_MAX_EVIDENCE]]
    if r.status == "unverified":
        lines[0] = f"unverified: {lines[0]}"
    result: dict[str, Any] = {
        "ruleId": r.rule.id,
        "ruleIndex": index,
        "level": "note" if r.status == "unverified" else _LEVELS[r.rule.severity],
        "message": {"text": "\n".join(lines)},
    }

    file_locations = [_location(ev.path, ev.line) for ev in r.evidence[:_MAX_EVIDENCE] if ev.path]
    if not file_locations:
        src = split_source(r.rule.source)
        if src:
            file_locations = [_location(*src)]
    if file_locations:
        result["locations"] = file_locations[:1]
        if len(file_locations) > 1:
            result["relatedLocations"] = [
                {"id": i, **loc} for i, loc in enumerate(file_locations[1:], start=1)
            ]
    return result


def _location(path: str, line: int | None) -> dict[str, Any]:
    physical: dict[str, Any] = {"artifactLocation": {"uri": quote(path.replace("\\", "/"))}}
    if line and line > 0:
        physical["region"] = {"startLine": line}
    return {"physicalLocation": physical}
