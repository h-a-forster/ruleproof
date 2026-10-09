"""Run rules against a context."""

from __future__ import annotations

from ruleproof.checks import REGISTRY, load_all
from ruleproof.models import Context, Rule, RuleResult

_INPUT_NAMES = {"diff": "no diff", "session": "no transcript"}


def run_rule(rule: Rule, ctx: Context) -> RuleResult:
    load_all()
    spec = REGISTRY.get(rule.check)
    if spec is None:  # the loader rejects unknown checks; this guards direct callers
        return RuleResult(rule, "skip", f"unknown check {rule.check!r}")
    present = {"diff": ctx.diff is not None, "session": ctx.session is not None}
    missing = sorted(n for n in spec.needs if not present[n])
    if spec.needs_any and len(missing) < len(spec.needs):
        missing = []
    if missing:
        return RuleResult(rule, "skip", ", ".join(_INPUT_NAMES[m] for m in missing))
    try:
        return spec.fn(rule, ctx)
    except Exception as exc:  # one broken rule must not hide the others
        return RuleResult(rule, "skip", f"check crashed: {type(exc).__name__}: {exc}")


def run_rules(rules: list[Rule], ctx: Context) -> list[RuleResult]:
    return [run_rule(r, ctx) for r in rules]
