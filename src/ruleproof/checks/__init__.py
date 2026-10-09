"""Check registry.

A check is a function ``(rule, ctx) -> RuleResult`` registered under a name with a parameter
schema. The rule loader validates ``Rule.params`` against the schema (and fills defaults)
before any check runs, so check functions can index ``rule.params`` directly.

Check modules register themselves on import; ``load_all()`` imports them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ruleproof.models import Context, Rule, RuleResult

Input = Literal["diff", "session"]
ParamType = Literal["str", "int", "bool", "str_list", "regex", "regex_list", "glob_list"]

CheckFn = Callable[[Rule, Context], RuleResult]


@dataclass(frozen=True, slots=True)
class Param:
    type: ParamType
    required: bool = False
    default: Any = None
    doc: str = ""


@dataclass(frozen=True, slots=True)
class CheckSpec:
    name: str
    fn: CheckFn
    needs: frozenset[Input]  # inputs that must all be present, else the rule is skipped
    params: dict[str, Param] = field(default_factory=dict)
    doc: str = ""  # one paragraph, shown by `ruleproof checks`
    needs_any: bool = False  # True: run when at least one of `needs` is present


REGISTRY: dict[str, CheckSpec] = {}


def register(
    name: str,
    *,
    needs: set[Input] | frozenset[Input],
    params: dict[str, Param] | None = None,
    doc: str = "",
    needs_any: bool = False,
) -> Callable[[CheckFn], CheckFn]:
    def deco(fn: CheckFn) -> CheckFn:
        if name in REGISTRY:
            raise ValueError(f"check {name!r} registered twice")
        REGISTRY[name] = CheckSpec(name, fn, frozenset(needs), dict(params or {}), doc, needs_any)
        return fn

    return deco


def load_all() -> dict[str, CheckSpec]:
    from ruleproof.checks import claims, diff_checks, transcript_checks  # noqa: F401

    return REGISTRY
