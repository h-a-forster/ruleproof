"""The ``claims`` check: success the agent reports must be backed by a command it ran.

Each ``Claim`` pairs phrases agents use to report a result ("All 42 tests pass", "ruff is
clean", "I've committed the changes") with a regex for commands that would produce it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ruleproof.checks import Param, register
from ruleproof.checks._common import (
    clip,
    command_matches,
    command_outcome,
    commands_run,
    compile_regex,
    counted_edits,
    exit_belongs_to,
    is_powershell,
    matches_invocation,
    output_verdict,
    quoted,
    ran_after,
    simple_commands,
    split_invocations,
)
from ruleproof.models import Context, Event, Evidence, Rule, RuleResult, Status


@dataclass(frozen=True, slots=True)
class Claim:
    name: str
    phrases: tuple[re.Pattern[str], ...]  # how agents report the result
    evidence: re.Pattern[str]  # the tool, matched in command position (see ``split_invocations``)
    description: str  # what is claimed, completing "claimed ..."
    command: str  # what is missing, completing "no ... ran"
    config: str | None = None  # param naming the project's own commands for this claim
    kind: str | None = None  # output signals that belong to it (see ``output_verdict``)


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


def _cmd(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# A verdict word must not be followed by an object ("the test passes an empty list", "the
# build passes the flag") or a participle ("types are cleanly separated").
_NOT_ARG = (
    r"(?!\s+(?:a|an|the|this|that|these|those|its|it|them|none|null|nil|true|false|through"
    r"|along|back|down|over|into|onto|to|from|\w+ed)\b|\s*[\[{\"']|\s+--?\w|\s+\w+\()"
)
_PASS = (
    r"(?:pass(?:es|ed|ing)?|green|succeed(?:s|ed)?|successful(?:ly)?|clean(?:ly)?|ok)\b" + _NOT_ARG
)
_TESTS_PASS = r"(?:pass(?:es|ed|ing)?|green|succeed(?:s|ed)?|ok)\b" + _NOT_ARG
_MARK = r"(?:✓|✔️?|✅|☑️?|\[x\])"
# Between a subject and its verdict: markdown emphasis, colons, arrows, table pipes, a short
# parenthetical ("suite (19 tests) passes"), then filler words ("are all now passing").
_GAP = (
    r"(?:\s*\([^()]{0,40}\))?[\s*_:|=>\-–—→]*"
    r"(?:(?:are|is|all|now|still|both|were|was|have|has|fully|again|also)\s+)*"
)
_NONE = (
    r"(?:(?:reports?|shows?|finds?|found|has|had|gives?)\s+)?no\s+(?:\w+\s+)?"
    r"(?:issues|errors|warnings|problems|violations|findings|complaints)"
)
_TEST_SUBJECT = r"(?:tests?|test\s+suite|suite|specs)"
_LINT_SUBJECT = (
    r"(?:lint(?:ing|er|s)?|ruff(?:\s+check)?|flake8|pylint|eslint|clippy|golangci-lint"
    r"|rubocop|biome(?:\s+(?:check|lint))?|oxlint|stylelint|shellcheck)"
)
_TYPES_SUBJECT = (
    r"(?:mypy(?:\s+--strict)?|pyright|basedpyright|pyre|tsc|typescript(?:\s+compiler)?"
    r"|type[- ]?check(?:s|ing|er)?|types|typing)"
)


def _marked(subject: str) -> tuple[str, str]:
    """``subject`` ticked off with a checkmark, before or after it."""
    return (
        rf"{_MARK}[\s*_:|]*(?:(?:all|the|full|unit)\s+)*{subject}\b",
        rf"\b{subject}\b[\s*_:|]*{_MARK}",
    )


_FORMATTERS = (
    r"(?:black|ruff\s+format|isort|prettier|biome\s+format|gofmt|goimports|rustfmt|cargo\s+fmt"
    r"|dotnet\s+format|clang-format|shfmt|deno\s+fmt|the\s+formatter)"
)
_RUN = r"(?:(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?|make\s+|just\s+|task\s+)"
# A project test script, run by path or through a shell: `./scripts/test.sh`, `.\run-tests.ps1`,
# `bash scripts/test.sh` (a bare `test` is the shell builtin).
_TEST_SCRIPT = (
    r"(?:(?:(?:ba|z)?sh|pwsh|powershell)\s+(?:-\w+\s+)*)?(?:\S*[\\/])?"
    r"(?:run[-_]?)?tests?(?:[-_]\w+)?\.(?:sh|ps1|bat|cmd)\b"
)
# A push claim names what was pushed or where to: "pushed the logic down" is not a push.
_GIT_OBJ = r"(?:changes?|commits?|branch(?:es)?|fix(?:es)?|work|it|them|tags?|everything)"
_REMOTE = (
    r"(?:origin\b(?:/[\w./-]+)?|upstream\b(?:/[\w./-]+)?|(?:the\s+)?remote(?![ \t]+\w)"
    r"|(?:the\s+)?remote[ \t]+branch|github|gitlab|bitbucket|(?:the\s+)?(?:main|master)\b"
    r"|the\s+PR\b|PR\s+#?\d+)"
)

CLAIMS: dict[str, Claim] = {
    c.name: c
    for c in (
        Claim(
            "tests",
            _rx(
                rf"\b{_TEST_SUBJECT}\b{_GAP}{_TESTS_PASS}",
                rf"\b{_TEST_SUBJECT}\b[^,;.]{{0,40}}\band\s+(?:they|it|all|both|everything)\s+"
                rf"(?:all\s+|now\s+)?{_TESTS_PASS}",
                r"\b\d+(?:\s*/\s*\d+)?\s+(?:(?:new|existing|unit)\s+)?(?:tests?\s+|specs?\s+"
                r"|examples?\s+)?(?:passed|passing)\b",
                r"\bpass(?:es|ed|ing)?\s+(?:all\s+)?(?:the\s+)?(?:\d+\s+)?(?:existing\s+)?tests\b",
                r"\b(?:0|zero|no)\s+(?:test\s+)?failures\b",
                rf"\b(?:pytest|jest|vitest|mocha|rspec|phpunit|go\s+test|cargo\s+test|unittest)"
                rf"\b{_GAP}(?:\d+\s+(?:tests?\s+)?)?{_TESTS_PASS}",
                *_marked(_TEST_SUBJECT),
            ),
            _cmd(
                r"\b(?:pytest|py\.test|nose2|unittest|ward|hatch\s+(?:run\s+)?test"
                r"|tox(?!\s+-[ef]\s+\S*(?:lint|type|mypy|format|fmt|docs|build))"
                r"|nox(?!\s+-s\s+\S*(?:lint|type|mypy|format|fmt|docs|build))"
                r"|jest|vitest|mocha|ava|karma|playwright\s+test|cypress\s+run"
                r"|go\s+test|cargo\s+(?:test|nextest)|gradlew?\b.*\b(?:test|check)"
                r"|mvnw?\b.*\b(?:test|verify)|dotnet\s+test|rspec|rake\s+(?:test|spec)"
                r"|rails\s+test|phpunit|pest|ctest|mix\s+test|swift\s+test|deno\s+test"
                r"|bun\s+test|Invoke-Pester|composer\s+test|bazel(?:isk)?\s+test"
                r"|(?:manage\.py|django-admin)\s+test|(?:run[-_]?)?tests?\.py"
                r"|" + _RUN + r"(?:test|t|spec|check))\b|" + _TEST_SCRIPT
            ),
            "tests pass",
            "test command",
            "test_commands",
            "tests",
        ),
        Claim(
            "lint",
            _rx(
                rf"\b{_LINT_SUBJECT}\b{_GAP}(?:{_PASS}|happy\b|{_NONE}\b)",
                *_marked(_LINT_SUBJECT),
                r"\bno\s+(?:new\s+|remaining\s+)?(?:lint(?:ing)?|ruff|eslint|flake8|pylint|clippy)\s+"
                r"(?:errors|warnings|issues|violations|problems|findings)\b",
                r"\blint[- ](?:clean|free)\b",
            ),
            _cmd(
                r"\b(?:ruff(?!\s+format)|flake8|pylint|pyflakes|pycodestyle|eslint"
                r"|biome\s+(?:check|lint|ci)|oxlint|stylelint|golangci-lint|go\s+vet"
                r"|staticcheck|clippy|rubocop|standardrb|phpcs|phpstan|psalm|ktlint|detekt"
                r"|checkstyle|shellcheck|hadolint|markdownlint|pre-commit\s+run"
                r"|dotnet\s+format\s+.*--verify-no-changes|(?:tox\s+-e|nox\s+-s)\s+\S*lint"
                r"|" + _RUN + r"lint)"
            ),
            "lint is clean",
            "lint command",
            "lint_commands",
            "lint",
        ),
        Claim(
            "types",
            _rx(
                rf"\b{_TYPES_SUBJECT}\b{_GAP}(?:{_PASS}|happy\b|{_NONE}\b)",
                *_marked(_TYPES_SUBJECT),
                r"\bno\s+(?:new\s+|remaining\s+)?(?:type|typing|mypy|pyright|tsc|typescript)\s+"
                r"(?:errors|issues|problems)\b",
                r"\btypes?\s+(?:now\s+)?(?:check\s+out|checks?\s+(?:cleanly|pass(?:es)?))\b",
                r"\btypes\s+(?:now\s+|all\s+)?check\s*(?:[.!,;)]|$)",
                r"\b(?:is|are|now)\s+type[- ]safe\b",
            ),
            _cmd(
                r"\b(?:mypy|dmypy|pyright|basedpyright|pyre|pytype|ty\s+check|tsc|vue-tsc"
                r"|tsgo|flow\s+check|cargo\s+check|phpstan|psalm|srb\s+tc|steep\s+check"
                r"|" + _RUN + r"(?:typecheck|type-check|types|tsc|mypy))\b"
            ),
            "types check",
            "type-check command",
            "type_commands",
            "types",
        ),
        Claim(
            "build",
            _rx(
                rf"\bbuild\b{_GAP}(?:succeeds|succeeded|pass(?:es|ed|ing)?|works|green|clean"
                rf"|successful|ok|completed?\s+successfully)\b{_NOT_ARG}",
                *_marked("build"),
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
            "build_commands",
            "build",
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
            "format_commands",
            "format",
        ),
        Claim(
            "commit",
            _rx(
                r"\b(?:I|we)(?:'ve|\s+have)?\s+(?:also\s+|now\s+|just\s+)?committed\b"
                r"(?!\s+(?:to|myself|ourselves)\b)",
                r"\bran\s+git\s+commit\b",
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
                rf"\bpushed\s+(?:(?:the|my|all|these|both|your|this)\s+)?(?:\w+\s+)?{_GIT_OBJ}\b",
                rf"\bpushed\s+(?:\S+\s+){{0,3}}?(?:up\s+)?to\s+{_REMOTE}",
                r"\bran\s+git\s+push\b",
                r"\b(?:changes|branch|commits?|fix)\s+(?:ha(?:ve|s)\s+been|are|were|is|was)"
                r"\s+pushed\b",
            ),
            _cmd(r"\b(?:git\s+(?:-[cC]\s+\S+\s+|--?[\w-]+(?:=\S+)?\s+)*|jj\s+git\s+)push\b"),
            "changes were pushed",
            "git push",
        ),
    )
}

# "Everything works and the fix is verified": a general success report, backed by the tests.
CLAIMS["works"] = Claim(
    "works",
    _rx(
        r"\b(?:everything|it\s+all|all\s+of\s+it)\s+(?:now\s+|still\s+)?works\b",
        r"\b(?:the\s+)?(?:fix|fixes|change|changes|feature|implementation|behaviou?r)\s+"
        r"(?:is|are|has\s+been|have\s+been)\s+(?:now\s+|fully\s+)?(?:verified|confirmed)\b",
        r"\bverified\s+(?:that\s+)?(?:the\s+fix|the\s+change|it|everything)\s+works\b",
    ),
    CLAIMS["tests"].evidence,
    "the change works",
    "test command",
    "test_commands",
    "tests",
)

# Cues that void a whole sentence: conditions, expectations, other people's reports,
# history, goals, and partial or mixed results.
_SENTENCE_HEDGE = re.compile(
    r"'ll\b|\b(?:if|unless|once|should|would|could|will|may|might|assum(?:e|es|ed|ing)"
    r"|believes?|believed|expect(?:s|ed)?|think|hopefully|probably|likely|presumably|seems?"
    r"|appears?|said|says|goal|criteri(?:on|a)|requirements?|todo|remaining|next\s+steps?"
    r"|to\s+verify|to\s+confirm|to\s+check|please\s+run|you\s+can|you\s+should|needs?\s+to"
    r"|going\s+to|ready\s+to|about\s+to|let\s+me|want\s+me|previously|used\s+to"
    r"|make\s+sure|ensure|must|has\s+to|have\s+to"
    r"|before\s+(?:my|the|this|these|your)\s+(?:change|changes|edit|edits|fix)|on\s+main"
    r"|in\s+ci|untested|without\s+running|except|mostly|partially|partly|whether)\b"
    r"|\b(?:most|some|only|several|many|few)\s+(?:of\s+(?:the\s+)?)?(?:\d+\s+)?(?:\w+\s+)?"
    r"tests?\b"
    r"|\b[1-9]\d*\s+(?:\w+\s+)?(?:tests?\s+)?(?:failed|fail|fails|failing|failures?)\b"
    r"|\bpassed,\s+[1-9]\d*\s+errors?\b"
    # "15 of 16 tests pass", "the other 19 tests pass": the rest did not.
    r"|\b\d+\s+(?:of|out\s+of)\s+\d+\s+(?:\w+\s+)?tests?\b"
    r"|\b(?:other|rest\s+of\s+the)\s+(?:\d+\s+)?(?:\w+\s+)?tests?\b"
    # "all 3 tests in test_totals.py pass": scoped to named files, not the suite.
    r"|\btests?\s+(?:in|from)\s+`?[\w/\\.-]+\.py\b",
    re.IGNORECASE,
)
# Cues that void only their own clause: negation and failure words.
_CLAUSE_NEGATION = re.compile(
    r"n't\b|\b(?:not|never|no\s+longer|unable|cannot|nothing|fail(?:s|ed|ing|ures?)?|errors?"
    r"|broken|red)\b|[\u2717\u2718\u274c]",
    re.IGNORECASE,
)
# "no type errors", "0 failures" are success reports, not failure cues.
_NO_PROBLEMS = re.compile(
    r"\b(?:no|0|zero|without)\s+(?:\w+\s+){0,2}?(?:errors?|failures?|failed|warnings|issues"
    r"|problems)\b",
    re.IGNORECASE,
)
# Quoted material is someone else's words: a rule ("make sure the tests pass"), an issue.
_QUOTED = re.compile(r"\"[^\"\n]{1,300}\"|\u201c[^\u201d\n]{1,300}\u201d")
_BLOCKQUOTE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", re.MULTILINE | re.DOTALL)
_UNCHECKED = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])?[ \t]*\[ \].*$", re.MULTILINE)
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
# Clauses are judged separately: in "the suite passes, and I didn't touch the tests" the
# negation belongs to the second clause only. "and they pass" stays with its subject.
_CLAUSES = re.compile(
    r"[,;]\s+|\s+(?:and|but|although|though|while|whereas)\s+"
    r"(?!(?:they|it|all|both|everything)\b)",
    re.IGNORECASE,
)
_PARENS = re.compile(r"\([^()]*\)")


def sentences(text: str) -> list[str]:
    """Sentences of assistant prose, without fenced code blocks, blockquotes, quoted text,
    unchecked checklist items (``- [ ] tests pass``) or inline-code backticks."""
    text = _UNCHECKED.sub("", _BLOCKQUOTE.sub("", _FENCE.sub("\n", text)))
    text = _QUOTED.sub(" ", text).replace("`", "")
    return [s.strip() for s in _SPLIT.split(text) if s.strip()]


def clauses(sentence: str) -> list[str]:
    """Clauses of a sentence; commas inside parentheses do not split."""
    protected = _PARENS.sub(lambda m: m.group().replace(",", "\0").replace(";", "\1"), sentence)
    return [c.replace("\0", ",").replace("\1", ";") for c in _CLAUSES.split(protected)]


def is_assertion(sentence: str) -> bool:
    """False for questions and conditional, hedged, second-hand or partial statements."""
    return not sentence.rstrip().endswith("?") and not _SENTENCE_HEDGE.search(sentence)


def find_claims(text: str, names: list[str]) -> dict[str, str]:
    """Claim name -> first sentence in ``text`` that makes it."""
    found: dict[str, str] = {}
    for sentence in sentences(text):
        if not is_assertion(sentence):
            continue
        for clause in clauses(sentence):
            if _CLAUSE_NEGATION.search(_NO_PROBLEMS.sub(" ", clause)):
                continue
            for name in names:
                if name not in found and any(rx.search(clause) for rx in CLAIMS[name].phrases):
                    found[name] = sentence
    return found


_DEFAULT_IGNORED = ["*.md", "*.rst", "*.txt"]
_RANK: dict[Status, int] = {"fail": 0, "unverified": 1, "pass": 2, "skip": 3}
_CONFIG_PARAMS = {c.config: c.description for c in CLAIMS.values() if c.config}

# Commands that print nothing a verdict could be read from: they do not muddy a chain.
_QUIET = frozenset(
    ["cd", "pushd", "popd", "set", "export", "source", ".", "echo", "printf", "tail", "head"]
    + ["true", "set-location", "sl", "write-host", "write-output", "out-null", "clear", "cls"]
    + ["activate", "deactivate", "sleep", "date", "pwd", "tee", "grep", "findstr", "more"]
    + ["select-string", "select-object", "sort", "wc"]
    + ["sh", "bash", "zsh", "dash", "ksh", "cmd", "pwsh", "powershell"]  # shells that run a script
)


_Hit = Callable[[list[str]], bool]
_Split = tuple[list[list[str]], list[list[str]], _Hit]


def _matching(claim: Claim, rule: Rule, ev: Event, ctx: Context) -> _Split | None:
    """(the simple commands of ``ev`` that run ``claim``'s tool, the others), or None when
    none does.

    Built-in tools must be in command position (``split_invocations``); the project's own
    commands (``test_commands`` ...) are searched anywhere in the command line, as by
    require-command.
    """
    ps = is_powershell(ev, ctx)
    hits, others = split_invocations(claim.evidence, ev.text, ps)
    if hits:
        return hits, others, lambda w: matches_invocation(claim.evidence, w)
    patterns = [compile_regex(p) for p in rule.params.get(claim.config or "") or []]
    if not any(command_matches(rx, ev.text, False, ps) for rx in patterns):
        return None
    words = simple_commands(ev.text, ps)
    own = [w for w in words if any(rx.search(" ".join(w)) for rx in patterns)]
    if not own:
        return words, [], lambda w: True
    return own, [w for w in words if w not in own], lambda w: w in own


def _program(word: str) -> str:
    return re.sub(r"\.exe$", "", re.split(r"[\\/]", word)[-1].lower())


def _outcome(
    claim: Claim, run: Event, others: list[list[str]], owns_exit: bool = True
) -> tuple[Status, str]:
    """``command_outcome``, read for the claimed tool when the exit code or output is shared.

    In ``ruff check . && ruff format --check .`` the exit code and the output belong to both
    commands: the verdict comes from the output signals of the claimed kind of tool (ruff's
    "All checks passed!" for lint), and is unverified when they say nothing. When the exit
    code is not the claimed tool's (``pytest | tail``, ``pytest; git status``,
    ``pytest || true``: ``owns_exit`` is False) only those output signals count.
    """
    outcome, reason = command_outcome(run)
    shared = claim.kind is not None and any(_program(w[0]) not in _QUIET for w in others)
    if owns_exit and not shared:
        return outcome, reason
    mine = output_verdict(run.output, claim.kind) if claim.kind else None
    if mine is False:
        return "fail", reason if outcome == "fail" else "its output shows failures"
    if owns_exit and run.exit_code == 0 and outcome == "pass":
        return outcome, reason
    if mine is True:
        return "pass", f"chained; its own output shows success ({reason})"
    if not owns_exit:
        return "unverified", "exit code belongs to another command"
    return "unverified", f"chained with other commands, {reason}; result not attributable"


# "All tests pass", "the full suite is green"; not "all 3 new tests pass".
_FULL_SUITE = re.compile(
    r"\b(?:full|whole|entire|complete)\b|\bsuite\b"
    r"|\b(?:all|every)\s+(?:of\s+)?(?:the\s+)?(?:\d+\s+)?(?:existing\s+|unit\s+)?(?:tests?|specs)\b",
    re.IGNORECASE,
)
_CLAIMED_COUNT = re.compile(r"\b(\d+)(?:\s*/\s*\d+)?\s+(?:\w+\s+)?tests?\b", re.IGNORECASE)
_RAN_COUNT = (
    re.compile(r"^.*?\b(\d+) passed\b.* in [\d.]+s\b", re.MULTILINE),  # pytest
    re.compile(r"^Ran (\d+) tests? in\b", re.MULTILINE),  # unittest
    re.compile(r"^\s*Tests?:?\s+.*?\b(\d+) passed\b", re.MULTILINE),  # jest / vitest
)
_SELECT_FLAGS = frozenset(
    ["-k", "--lf", "--last-failed", "--deselect", "-t", "--testNamePattern", "-run", "--grep"]
)


def _subset(own: list[list[str]], output: str, sentence: str) -> str | None:
    """Why a test run backs only part of a full-suite claim ("All 120 tests pass"), or None.

    A run is partial when it selects tests (``pytest tests/test_app.py``, ``-k``, ``--lf``,
    ``python -m unittest tests.test_app``, ``jest src/a.test.ts``) or reports fewer tests
    than the claim names.
    """
    claimed = _CLAIMED_COUNT.search(sentence)
    for rx in _RAN_COUNT:
        m = rx.search(output)
        if m and claimed and int(m.group(1)) < int(claimed.group(1)):
            return f"it ran {m.group(1)} tests, the claim names {claimed.group(1)}"
    if not _FULL_SUITE.search(sentence):
        return None
    for words in own:
        args = words[1:]
        if _SELECT_FLAGS.intersection(args):
            return "it selects tests"
        tool = " ".join(words[:2]).lower()
        operands = [a for a in args if not a.startswith("-")]
        if words[0].lower() in ("unittest",) and operands and "discover" not in operands:
            return "it runs named test modules"
        if any("::" in a or re.search(r"\.(?:py|[jt]sx?|rb)$", a) for a in operands):
            return "it runs named test files"
        if tool.startswith("cargo test") and [a for a in operands[1:] if a != "--"]:
            return "it filters tests by name"
    return None


@register(
    "claims",
    needs={"session"},
    params={
        "claims": Param(
            "str_list",
            default=list(CLAIMS),
            choices=tuple(CLAIMS),
            doc=f"claims to verify: {', '.join(CLAIMS)}",
        ),
        "ignore_edit_paths": Param(
            "glob_list", default=_DEFAULT_IGNORED, doc="edits that do not reset the evidence"
        ),
        **{
            name: Param(
                "regex_list",
                default=[],
                doc=f"the project's own commands that show {what} (searched anywhere in the "
                "command line, as by require-command), besides the built-in tools",
            )
            for name, what in _CONFIG_PARAMS.items()
        },
    },
    doc=(
        "Fails when the agent reports a result (tests pass, lint is clean, types check, the "
        "build succeeds, code is formatted, changes were committed or pushed, the change "
        "works) that no successful command backs up. Only the main agent's messages after "
        "its last counted edit are read (the final message when it made no edits); fenced "
        "code blocks, blockquotes, quoted text, unchecked checklist items, questions and "
        "negated, hedged, conditional, second-hand, partial or prescriptive statements "
        '("I couldn\'t run the tests", "tests should pass", "once you run pytest", "40 pass '
        'and 2 fail", "make sure the tests pass") are ignored. The evidence is the last '
        "command after that edit (by the agent or a subagent, not denied or blocked) that "
        "runs the tool in command position (`uv run pytest`, `.venv\\Scripts\\pytest.exe`, "
        "not `cat pytest.ini` or `pytest --version`): exit 0 backs the claim unless the "
        "output shows failures, a failure contradicts it, an unknown exit code is judged "
        "from the output or reported as unverified. When the tool is chained with other "
        "commands (`ruff check . && ruff format --check .`) the shared exit code and output "
        "are read for the claimed tool's own summary line; without one the claim is "
        "unverified; after `|`, `;` or `||` (`pytest | tail -3`) the exit code is another "
        "command's and only that summary line counts. "
        'A full-suite claim ("all 120 tests pass") backed only by a run that '
        "selects tests (`pytest tests/test_app.py`, `-k`) or reports fewer tests than "
        "claimed is unverified. `test_commands`, `lint_commands`, `type_commands`, "
        "`build_commands` and `format_commands` add the project's own commands. Edits are "
        "file-tool edits and shell commands that obviously write files; those matching "
        "`ignore_edit_paths` (docs by default) do not count."
    ),
)
def claims_check(rule: Rule, ctx: Context) -> RuleResult:
    assert ctx.session is not None
    names: list[str] = rule.params["claims"]
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
    commands = [e for e in commands_run(ctx) if last_edit is None or ran_after(e, last_edit)]
    evidence: list[Evidence] = []
    verdicts: list[tuple[Status, str]] = []
    for name, (msg, sentence) in made.items():
        claim = CLAIMS[name]
        evidence.append(
            Evidence(f"claimed {claim.description}", event=msg.index, excerpt=clip(sentence))
        )
        runs = [(e, split) for e in commands if (split := _matching(claim, rule, e, ctx))]
        if not runs:
            note = f"no {claim.command} ran{after}"
            evidence.append(Evidence(note))
            verdicts.append(("fail", f"claimed {claim.description}; {note}"))
            continue
        run, (own, others, hit) = runs[-1]
        owns_exit = exit_belongs_to(run.text, is_powershell(run, ctx), hit)
        outcome, reason = _outcome(claim, run, others, owns_exit)
        partial = _subset(own, run.output, sentence) if claim.kind == "tests" else None
        if outcome == "pass" and partial:
            outcome, reason = "unverified", f"partial run: {partial}"
        evidence.append(Evidence(f"ran {quoted(run.text)} ({reason})", event=run.index))
        if partial and outcome == "unverified":
            text = (
                f"claimed {claim.description}; {quoted(run.text)} backs only part of the "
                f"claim ({partial})"
            )
        elif outcome == "fail":
            text = f"claimed {claim.description}; {quoted(run.text)} failed ({reason})"
        elif outcome == "unverified" and reason == "exit code unknown":
            text = (
                f"claimed {claim.description}; {quoted(run.text)} ran but its exit code is unknown"
            )
        elif outcome == "unverified":
            text = f"claimed {claim.description}; {quoted(run.text)} ran but {reason}"
        else:
            text = f"{name} ({reason})"
        verdicts.append((outcome, text))

    status = min((s for s, _ in verdicts), key=_RANK.__getitem__)
    if status == "pass":
        summary = "claims backed by commands: " + ", ".join(t for _, t in verdicts)
    else:
        summary = "; ".join(t for s, t in verdicts if s == status)
    return RuleResult(rule, status, summary, evidence)
