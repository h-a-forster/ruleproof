"""The ``claims`` check: success the agent reports must be backed by a command it ran.

Each ``Claim`` pairs phrases agents use to report a result ("All 42 tests pass", "ruff is
clean", "I've committed the changes") with a regex for commands that would produce it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ruleproof.checks import Param, register
from ruleproof.checks._common import (
    clip,
    command_matches,
    command_outcome,
    counted_edits,
    quoted,
)
from ruleproof.models import Context, Event, Evidence, Rule, RuleResult, Status


@dataclass(frozen=True, slots=True)
class Claim:
    name: str
    phrases: tuple[re.Pattern[str], ...]  # how agents report the result
    evidence: re.Pattern[str]  # a command that would show it
    description: str  # what is claimed, completing "claimed ..."
    command: str  # what is missing, completing "no ... ran"


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


def _cmd(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


_PASS = r"(?:pass(?:es|ed|ing)?|green|succeed(?:s|ed)?|successful(?:ly)?|clean(?:ly)?|ok)"
_TESTS_PASS = r"(?:pass(?:es|ed|ing)?|green|succeed(?:s|ed)?)"
_FORMATTERS = (
    r"(?:black|ruff\s+format|isort|prettier|biome\s+format|gofmt|goimports|rustfmt|cargo\s+fmt"
    r"|dotnet\s+format|clang-format|shfmt|deno\s+fmt|the\s+formatter)"
)
_RUN = r"(?:(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?|make\s+|just\s+|task\s+)"

CLAIMS: dict[str, Claim] = {
    c.name: c
    for c in (
        Claim(
            "tests",
            _rx(
                rf"\btests?\b[^.;:]{{0,40}}\b{_TESTS_PASS}\b",
                rf"\btest\s+suite\b[^.;:]{{0,30}}\b{_TESTS_PASS}\b",
                r"\b\d+\s+(?:tests?\s+|specs?\s+|examples?\s+)?passed\b",
                r"\bpass(?:es|ed|ing)?\s+(?:all\s+)?(?:the\s+)?(?:\d+\s+)?(?:existing\s+)?tests\b",
                r"\b(?:0|zero|no)\s+(?:test\s+)?failures\b",
                rf"\b(?:pytest|jest|vitest|mocha|rspec|phpunit|go test|cargo test|"
                rf"unittest)\b[^.;]{{0,30}}\b{_TESTS_PASS}\b",
            ),
            _cmd(
                r"\b(?:pytest|py\.test|tox|nox|nose2|unittest|ward|hatch\s+(?:run\s+)?test"
                r"|jest|vitest|mocha|ava|karma|playwright\s+test|cypress\s+run"
                r"|go\s+test|cargo\s+(?:test|nextest)|gradlew?\b.*\b(?:test|check)"
                r"|mvnw?\b.*\b(?:test|verify)|dotnet\s+test|rspec|rake\s+(?:test|spec)"
                r"|rails\s+test|phpunit|pest|ctest|mix\s+test|swift\s+test|deno\s+test"
                r"|bun\s+test|Invoke-Pester|composer\s+test|" + _RUN + r"(?:test|t|spec|check))\b"
            ),
            "tests pass",
            "test command",
        ),
        Claim(
            "lint",
            _rx(
                rf"\b(?:lint(?:ing|er|s)?|ruff(?!\s+format)|flake8|pylint|eslint|clippy"
                rf"|golangci-lint|rubocop|biome|oxlint|stylelint|shellcheck)\b[^.;]{{0,30}}"
                rf"\b(?:{_PASS}|happy|no\s+(?:issues|errors|warnings|problems))\b",
                r"\bno\s+(?:remaining\s+)?(?:lint(?:ing)?|ruff|eslint|flake8|pylint|clippy)\s+"
                r"(?:errors|warnings|issues|violations|problems|findings)\b",
                r"\blint[- ](?:clean|free)\b",
            ),
            _cmd(
                r"\b(?:ruff(?!\s+format)|flake8|pylint|pyflakes|pycodestyle|eslint"
                r"|biome\s+(?:check|lint|ci)|oxlint|stylelint|golangci-lint|go\s+vet"
                r"|staticcheck|clippy|rubocop|standardrb|phpcs|phpstan|psalm|ktlint|detekt"
                r"|checkstyle|shellcheck|hadolint|markdownlint|pre-commit\s+run"
                r"|dotnet\s+format\s+.*--verify-no-changes|" + _RUN + r"lint)"
            ),
            "lint is clean",
            "lint command",
        ),
        Claim(
            "types",
            _rx(
                rf"\b(?:mypy|pyright|basedpyright|pyre|tsc|typescript\s+compiler"
                rf"|type[- ]?check(?:s|ing|er)?)\b[^.;]{{0,30}}\b(?:{_PASS}|happy"
                rf"|no\s+(?:issues|errors))\b",
                r"\bno\s+(?:remaining\s+)?(?:type|typing|mypy|pyright|tsc|typescript)\s+"
                r"(?:errors|issues|problems)\b",
                r"\btypes?\s+(?:now\s+)?(?:check\s+out|checks?\s+(?:cleanly|pass(?:es)?))\b",
                r"\b(?:is|are|now)\s+type[- ]safe\b",
            ),
            _cmd(
                r"\b(?:mypy|dmypy|pyright|basedpyright|pyre|pytype|ty\s+check|tsc|vue-tsc"
                r"|tsgo|flow\s+check|cargo\s+check|phpstan|psalm|srb\s+tc|steep\s+check"
                r"|" + _RUN + r"(?:typecheck|type-check|types|tsc|mypy))\b"
            ),
            "types check",
            "type-check command",
        ),
        Claim(
            "build",
            _rx(
                r"\bbuild\s+(?:now\s+|still\s+)?(?:succeeds|succeeded|passes|passed|works"
                r"|is\s+(?:green|clean|successful|passing)|completed?\s+successfully)\b",
                r"\b(?:builds|compiles)\s+(?:successfully|cleanly|fine|without\s+"
                r"(?:errors|warnings))\b",
                r"\b(?:it|code|project|everything|crate|app|package)\s+(?:now\s+|still\s+)?"
                r"(?:compiles|builds)\b",
                r"\bsuccessfully\s+(?:built|compiled)\b",
                r"\bcompilation\s+(?:succeeds|succeeded|is\s+clean|passes)\b",
            ),
            _cmd(
                r"\b(?:cargo\s+(?:build|check)|go\s+build|vite\s+build|next\s+build|webpack"
                r"|rollup|esbuild|tsc|gradlew?\b.*\b(?:build|assemble|compile\w*)"
                r"|mvnw?\b.*\b(?:package|compile|install|verify)|dotnet\s+build|msbuild"
                r"|cmake\s+--build|ninja|bazel\s+build|python3?\s+-m\s+build|uv\s+build"
                r"|poetry\s+build|hatch\s+build|swift\s+build|xcodebuild|gem\s+build"
                r"|docker\s+build|" + _RUN + r"build|make(?:\s+(?:all|build|release|dist))?\s*$)"
            ),
            "the build succeeds",
            "build command",
        ),
        Claim(
            "format",
            _rx(
                r"\b(?:re)?formatted\s+(?:\w+\s+){0,3}(?:with|using|via|by\s+running)\s+"
                r"(?:ruff|biome|" + _FORMATTERS + ")",
                r"\b" + _FORMATTERS + r"\b[^.;]{0,30}\b(?:clean|pass(?:es|ed)?|happy|applied"
                r"|no\s+changes|unchanged)\b",
                r"\b(?:ran|applied)\s+" + _FORMATTERS + r"\b",
                r"\bformatting\s+(?:is\s+)?(?:clean|passes|applied|fixed|consistent)\b",
                r"\b(?:code|files?)\s+(?:is|are|was|were)\s+(?:properly\s+)?formatted\b",
            ),
            _cmd(
                r"\b(?:black|ruff\s+format|isort|autopep8|yapf|prettier|biome\s+(?:format|check)"
                r"|dprint|gofmt|goimports|gofumpt|rustfmt|cargo\s+fmt|dotnet\s+format"
                r"|clang-format|google-java-format|ktlint\s+(?:-F|--format)|spotless\w*"
                r"|rubocop\s+-[aA]|php-cs-fixer|mix\s+format|swiftformat|shfmt|deno\s+fmt"
                r"|pre-commit\s+run|" + _RUN + r"(?:format|fmt|prettier))"
            ),
            "code is formatted",
            "formatter",
        ),
        Claim(
            "commit",
            _rx(
                r"\b(?:I|we)(?:'ve|\s+have)?\s+(?:also\s+|now\s+|just\s+)?committed\b",
                r"\bcommitted\s+(?:the|all|these|this|your|my)\s+(?:\w+\s+)?"
                r"(?:changes|fix|fixes|work|files|code|update|updates)\b",
                r"\b(?:created|made)\s+(?:a\s+|the\s+|one\s+)?(?:new\s+)?commit\b",
                r"\b(?:changes|work|fix)\s+(?:ha(?:ve|s)\s+been|are|were|is|was)\s+committed\b",
                r"\bcommitted\s+(?:as|in)\s+`?[0-9a-f]{7,40}\b",
            ),
            _cmd(r"\bgit\s+(?:-[cC]\s+\S+\s+|--?[\w-]+(?:=\S+)?\s+)*commit\b"),
            "changes were committed",
            "git commit",
        ),
        Claim(
            "push",
            _rx(
                r"\bpushed\b[^.;]{0,40}\b(?:to|origin|remote|upstream|github|gitlab)\b",
                r"\b(?:I|we)(?:'ve|\s+have)?\s+(?:also\s+|now\s+|just\s+)?pushed\b",
                r"\b(?:changes|branch|commits?|fix)\s+(?:ha(?:ve|s)\s+been|are|were|is|was)"
                r"\s+pushed\b",
            ),
            _cmd(r"\b(?:git\s+(?:-[cC]\s+\S+\s+|--?[\w-]+(?:=\S+)?\s+)*|jj\s+git\s+)push\b"),
            "changes were pushed",
            "git push",
        ),
    )
}

_HEDGE = re.compile(
    r"n't\b|\b(?:not|never|unable|cannot|should|would|could|will|may|might|if|unless|once"
    r"|assuming|expect(?:ed)?\s+to|hopefully|probably|likely|yet|need(?:s)?\s+to"
    r"|going\s+to|ready\s+to|about\s+to|let\s+me|to\s+verify|to\s+confirm|to\s+check"
    r"|please\s+run|you\s+can|you\s+should|next\s+steps?|todo)\b|'ll\b"
    r"|\b[1-9]\d*\s+(?:tests?\s+)?(?:failed|failing|failures|errors)\b",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", re.MULTILINE | re.DOTALL)
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text: str) -> list[str]:
    """Sentences of assistant prose, without fenced code blocks or inline-code backticks."""
    text = _FENCE.sub("\n", text).replace("`", "")
    return [s.strip() for s in _SPLIT.split(text) if s.strip()]


def is_assertion(sentence: str) -> bool:
    """False for negated, hedged, conditional or future statements and for questions."""
    return not sentence.rstrip().endswith("?") and not _HEDGE.search(sentence)


def find_claims(text: str, names: list[str]) -> dict[str, str]:
    """Claim name -> first sentence in ``text`` that makes it."""
    found: dict[str, str] = {}
    for sentence in sentences(text):
        if not is_assertion(sentence):
            continue
        for name in names:
            if name not in found and any(rx.search(sentence) for rx in CLAIMS[name].phrases):
                found[name] = sentence
    return found


_DEFAULT_IGNORED = ["*.md", "*.rst", "*.txt"]
_RANK: dict[Status, int] = {"fail": 0, "unverified": 1, "pass": 2, "skip": 3}


@register(
    "claims",
    needs={"session"},
    params={
        "claims": Param(
            "str_list", default=list(CLAIMS), doc=f"claims to verify: {', '.join(CLAIMS)}"
        ),
        "ignore_edit_paths": Param(
            "glob_list", default=_DEFAULT_IGNORED, doc="edits that do not reset the evidence"
        ),
    },
    doc=(
        "Fails when the agent reports a result (tests pass, lint is clean, types check, the "
        "build succeeds, code is formatted, changes were committed or pushed) that no "
        "successful command backs up. Only the main agent's messages after its last counted "
        "edit are read (the final message when it made no edits); fenced code blocks, "
        'questions and negated, hedged or conditional sentences ("I couldn\'t run the tests", '
        '"tests should pass", "once you run pytest") are ignored. The evidence is the last '
        "matching command (by the agent or a subagent) after that edit: exit 0 backs the "
        "claim, a failure contradicts it, an unknown exit code is judged from the output or "
        "reported as unverified. Edits matching `ignore_edit_paths` (docs by default) do not "
        "count as edits."
    ),
)
def claims_check(rule: Rule, ctx: Context) -> RuleResult:
    assert ctx.session is not None
    names: list[str] = rule.params["claims"]
    unknown = [n for n in names if n not in CLAIMS]
    if unknown:
        return RuleResult(
            rule, "skip", f"unknown claim(s): {', '.join(unknown)} (known: {', '.join(CLAIMS)})"
        )
    session = ctx.session
    edits = counted_edits(ctx, rule.scope, None, rule.params["ignore_edit_paths"])
    last_edit = edits[-1][0] if edits else None
    if last_edit is None:
        final = session.final_message()
        messages = [final] if final else []
    else:
        messages = [
            e
            for e in session.assistant_messages()
            if e.actor == "main" and e.index > last_edit.index
        ]

    made: dict[str, tuple[Event, str]] = {}
    for msg in messages:
        for name, sentence in find_claims(msg.text, names).items():
            made.setdefault(name, (msg, sentence))
    if not made:
        return RuleResult(rule, "pass", "no verifiable claims")

    after = f" after edit #{last_edit.index}" if last_edit else ""
    commands = [e for e in session.commands() if last_edit is None or e.index > last_edit.index]
    evidence: list[Evidence] = []
    verdicts: list[tuple[Status, str]] = []
    for name, (msg, sentence) in made.items():
        claim = CLAIMS[name]
        evidence.append(
            Evidence(f"claimed {claim.description}", event=msg.index, excerpt=clip(sentence))
        )
        runs = [e for e in commands if command_matches(claim.evidence, e.text)]
        if not runs:
            note = f"no {claim.command} ran{after}"
            evidence.append(Evidence(note))
            verdicts.append(("fail", f"claimed {claim.description}; {note}"))
            continue
        run = runs[-1]
        outcome, reason = command_outcome(run)
        evidence.append(Evidence(f"ran {quoted(run.text)} ({reason})", event=run.index))
        if outcome == "fail":
            text = f"claimed {claim.description}; {quoted(run.text)} failed ({reason})"
        elif outcome == "unverified":
            text = (
                f"claimed {claim.description}; {quoted(run.text)} ran but its exit code is unknown"
            )
        else:
            text = f"{name} ({reason})"
        verdicts.append((outcome, text))

    status = min((s for s, _ in verdicts), key=_RANK.__getitem__)
    if status == "pass":
        summary = "claims backed by commands: " + ", ".join(t for _, t in verdicts)
    else:
        summary = "; ".join(t for s, t in verdicts if s == status)
    return RuleResult(rule, status, summary, evidence)
