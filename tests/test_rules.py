from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ruleproof import rules as rules_mod
from ruleproof.checks import CheckSpec, Param
from ruleproof.errors import ConfigError
from ruleproof.models import Context, Rule, RuleResult
from ruleproof.rules import (
    load_rules,
    parse_inline,
    parse_pyproject_toml,
    parse_rules_toml,
    validate,
)

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "rules" / "repo"


def _noop(rule: Rule, ctx: Context) -> RuleResult:
    return RuleResult(rule, "pass", "ok")


DEMO_CHECKS = {
    "demo-paths": CheckSpec(
        "demo-paths",
        _noop,
        frozenset({"diff"}),
        {
            "paths": Param("glob_list", required=True, doc="files to protect"),
            "except": Param("glob_list", default=[]),
        },
    ),
    "demo-all": CheckSpec(
        "demo-all",
        _noop,
        frozenset({"session"}),
        {
            "pattern": Param("regex", required=True),
            "patterns": Param("regex_list", default=[]),
            "flag": Param("bool", default=True),
            "limit": Param("int", default=None),
            "label": Param("str", default="x"),
            "names": Param("str_list", default=["a"]),
        },
    ),
    "demo-strict": CheckSpec(
        "demo-strict",
        _noop,
        frozenset({"diff"}),
        {
            "mode": Param("str", default="fast", choices=("fast", "slow")),
            "actions": Param("str_list", default=["add"], choices=("add", "modify", "delete")),
            "paths": Param("glob_list", default=None, nonempty=True),
            "max_files": Param("int", default=None),
        },
        one_of=("paths", "max_files"),
    ),
}


@pytest.fixture(autouse=True)
def demo_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate against throwaway checks only, never the real registry."""
    monkeypatch.setattr(rules_mod, "load_all", lambda: dict(DEMO_CHECKS))


def rule(**kw: object) -> Rule:
    base: dict[str, object] = {"id": "r1", "check": "demo-all", "origin": "x.toml:4"}
    base.update(kw)
    return Rule(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------ load_rules


def test_load_rules_fixture_repo() -> None:
    loaded = {r.id: r for r in load_rules(FIXTURE_REPO)}
    assert list(loaded) == ["no-backups", "tests-run", "agents-l4", "run-tests", "pkg/agents-l3"]

    nb = loaded["no-backups"]
    assert nb.origin == "ruleproof.toml:5"
    assert nb.source == "AGENTS.md:3"
    assert nb.params == {"paths": ["*.bak", "*.orig"], "except": []}

    tr = loaded["tests-run"]
    assert tr.origin == "ruleproof.toml:12"
    assert tr.severity == "warning"
    assert tr.params["pattern"] == r"\bpytest\b"
    assert tr.params["flag"] is False
    assert tr.params["limit"] == 3
    assert tr.params["names"] == ["a"]

    gen = loaded["agents-l4"]
    assert gen.params["paths"] == ["api/gen/"]
    assert gen.description == "Never edit generated code in `api/gen/`."
    assert (gen.source, gen.origin, gen.scope) == ("AGENTS.md:4", "AGENTS.md:4", None)

    run = loaded["run-tests"]
    assert run.description == "Run the tests before you finish."
    assert run.params["pattern"] == "py test"
    assert run.params["flag"] is True
    assert type(run.params["pattern"]) is str

    pkg = loaded["pkg/agents-l3"]
    assert pkg.scope == "pkg"
    assert pkg.severity == "info"
    assert pkg.params["paths"] == ["vendor/", "third party/"]
    assert pkg.description == "Keep `pkg` free of vendored copies."


def test_load_rules_without_inline() -> None:
    assert [r.id for r in load_rules(FIXTURE_REPO, inline=False)] == ["no-backups", "tests-run"]


def test_load_rules_discovery_order(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[[tool.ruleproof.rule]]\nid = "from-pyproject"\n'
        'check = "demo-paths"\npaths = "a"\n',
        encoding="utf-8",
    )
    found = load_rules(tmp_path, inline=False)
    assert [(r.id, r.origin) for r in found] == [("from-pyproject", "pyproject.toml:4")]
    assert found[0].params["paths"] == ["a"]

    (tmp_path / ".ruleproof.toml").write_text(
        '[[rule]]\nid = "hidden"\ncheck = "demo-paths"\npaths = ["b"]\n', encoding="utf-8"
    )
    assert [r.id for r in load_rules(tmp_path, inline=False)] == ["hidden"]


def test_load_rules_pyproject_without_table_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    assert load_rules(tmp_path, inline=False) == []
    (tmp_path / "pyproject.toml").write_text("[tool.x\n# ruleproof\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"^pyproject.toml:1: invalid TOML"):
        load_rules(tmp_path, inline=False)


def test_load_rules_explicit_file(tmp_path: Path) -> None:
    custom = tmp_path / "conf" / "mine.toml"
    custom.parent.mkdir()
    custom.write_text('[[rule]]\nid = "c"\ncheck = "demo-paths"\npaths = "x"\n', encoding="utf-8")
    assert [r.origin for r in load_rules(tmp_path, custom, inline=False)] == ["conf/mine.toml:1"]

    with pytest.raises(ConfigError, match="rules file not found: .*nope.toml"):
        load_rules(tmp_path, tmp_path / "nope.toml")

    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"pyproject.toml: no \[tool.ruleproof\] table"):
        load_rules(tmp_path, py)


def test_load_rules_duplicate_ids_across_sources(tmp_path: Path) -> None:
    (tmp_path / "ruleproof.toml").write_text(
        '[[rule]]\nid = "agents-l2"\ncheck = "demo-paths"\npaths = "x"\n', encoding="utf-8"
    )
    (tmp_path / "AGENTS.md").write_text(
        "Do not touch x.\n<!-- ruleproof: demo-paths paths=x -->\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as exc:
        load_rules(tmp_path)
    assert str(exc.value) == (
        "AGENTS.md:2: duplicate rule id 'agents-l2', already defined at ruleproof.toml:1; "
        "rule ids must be unique, rename one of them"
    )


def test_load_rules_reports_annotation_errors_with_location(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE_REPO, tmp_path / "repo")
    with (tmp_path / "repo" / "pkg" / "AGENTS.md").open("a", encoding="utf-8") as fh:
        fh.write("\n<!-- ruleproof: demo-pathz paths=y -->\n")
    with pytest.raises(
        ConfigError, match=r"^pkg/AGENTS.md:9: rule 'pkg/agents-l9': unknown check 'demo-pathz'"
    ):
        load_rules(tmp_path / "repo")


# ------------------------------------------------------------------------------------ TOML


def test_parse_rules_toml_version() -> None:
    assert parse_rules_toml("", "r.toml") == []
    assert parse_rules_toml("version = 1\n", "r.toml") == []
    with pytest.raises(ConfigError) as exc:
        parse_rules_toml("# hi\nversion = 2\n", "r.toml")
    assert str(exc.value) == "r.toml:2: unsupported version = 2; this ruleproof reads version = 1"
    with pytest.raises(ConfigError, match="unsupported version = true"):
        parse_rules_toml("version = true\n", "r.toml")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("x = \n", r"^r.toml:1: invalid TOML: .*line 1"),
        ("[[rules]]\nid = 'a'\n", r"^r.toml:1: unknown key rules \(the array is \[\[rule\]\]"),
        ("[rule]\nid = 'a'\n", r"^r.toml:1: rule must be an array of tables"),
        ("\n[[rule]]\ncheck = 'demo-all'\n", r"^r.toml:2: \[\[rule\]\] #1 has no id"),
        ("[[rule]]\nid = 'a'\n", r"^r.toml:1: \[\[rule\]\] #1 has no check"),
        (
            "[[rule]]\nid = 'a'\ncheck = 'demo-all'\nseverity = 1\n",
            r"^r.toml:1: \[\[rule\]\] #1: severity must be a string, found integer 1",
        ),
    ],
)
def test_parse_rules_toml_errors(text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_rules_toml(text, "r.toml")


def test_parse_rules_toml_inline_array_has_no_line() -> None:
    (r,) = parse_rules_toml('rule = [{id = "a", check = "demo-all", pattern = "x"}]', "r.toml")
    assert r.origin == "r.toml"
    assert r.params == {"pattern": "x"}


def test_parse_pyproject_toml() -> None:
    assert parse_pyproject_toml('[project]\nname = "x"\n', "pyproject.toml") is None
    text = '[tool.ruleproof]\nversion = 1\n\n[[tool.ruleproof.rule]]\nid = "a"\ncheck = "c"\n'
    (r,) = parse_pyproject_toml(text, "pyproject.toml") or []
    assert (r.id, r.origin) == ("a", "pyproject.toml:4")
    with pytest.raises(ConfigError, match="pyproject.toml:2: unsupported tool.ruleproof.version"):
        parse_pyproject_toml("[tool.ruleproof]\nversion = 9\n", "pyproject.toml")
    with pytest.raises(ConfigError, match=r"unknown key tool.ruleproof.rules"):
        parse_pyproject_toml("[tool.ruleproof]\nrules = []\n", "pyproject.toml")


# ---------------------------------------------------------------------------------- inline


def test_parse_inline_defaults_and_overrides() -> None:
    text = (
        "## Style\n"
        "\n"
        "1. *Always* run the __linter__ on snake_case_names.\n"
        "\n"
        "<!-- ruleproof: demo-all pattern='a b' id=lint severity=warning "
        'description="Run the linter" scope=src label="say \\"hi\\"" -->\n'
        "<!-- ruleproof: demo-all pattern=x -->\n"
    )
    first, second = parse_inline(text, "docs/CLAUDE.local.md")
    assert first.id == "lint"
    assert first.severity == "warning"
    assert first.description == "Run the linter"
    assert first.scope == "src"
    assert first.params == {"pattern": "a b", "label": 'say "hi"'}
    assert second.id == "docs/claude.local-l6"
    assert second.description == "Always run the linter on snake_case_names."
    assert second.scope == "docs"
    assert second.source == "docs/CLAUDE.local.md:6"


def test_parse_inline_scope_only_for_nested_instruction_files() -> None:
    text = "Rule.\n<!-- ruleproof: demo-paths paths=a -->\n"
    (gh,) = parse_inline(text, ".github/copilot-instructions.md")
    assert (gh.id, gh.scope) == ("github/copilot-instructions-l2", None)
    (cur,) = parse_inline(text, ".cursor/rules/style.mdc")
    assert cur.scope is None


def test_parse_inline_ignores_code() -> None:
    text = (
        "Example: `<!-- ruleproof: nope -->` and ``<!-- ruleproof: x` -->``.\n"
        "~~~~\n"
        "```\n"
        "<!-- ruleproof: nope -->\n"
        "~~~~~\n"
        "<!-- other comment -->\n"
    )
    assert parse_inline(text, "AGENTS.md") == []


def test_parse_inline_crlf() -> None:
    (r,) = parse_inline("Keep it.\r\n<!-- ruleproof: demo-paths paths=a -->\r\n", "AGENTS.md")
    assert (r.id, r.description) == ("agents-l2", "Keep it.")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("\n<!-- ruleproof: demo-all x=1", r"^A.md:2: unterminated ruleproof annotation"),
        ("<!-- ruleproof: -->", r"^A.md:1: annotation has no check name"),
        ("<!-- ruleproof: a=b -->", r"^A.md:1: annotation has no check name"),
        ("<!-- ruleproof: c paths -->", r"^A.md:1: expected key=value .* found 'paths'"),
        ("<!-- ruleproof: c 9x=1 -->", r"^A.md:1: invalid parameter name '9x'"),
        ("<!-- ruleproof: c a=1 a=2 -->", r"^A.md:1: parameter 'a' is given twice"),
        ('<!-- ruleproof: c a="open -->', r'^A.md:1: unterminated " in value of \'a\''),
        ("<!-- ruleproof: c check=d -->", r"^A.md:1: the check is the first word"),
    ],
)
def test_parse_inline_errors(text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_inline(text, "A.md")


# -------------------------------------------------------------------------------- validate


def test_validate_fills_defaults_and_coerces() -> None:
    out = validate(rule(params={"pattern": "a+", "names": "solo", "patterns": "x|y"}))
    assert out.params == {
        "pattern": "a+",
        "patterns": ["x|y"],
        "flag": True,
        "limit": None,
        "label": "x",
        "names": ["solo"],
    }
    assert DEMO_CHECKS["demo-all"].params["names"].default == ["a"]  # defaults are copied
    out.params["names"].append("b")
    assert validate(rule(params={"pattern": "a"})).params["names"] == ["a"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("yes", True), ("No", False), ("1", True), ("0", False), ("TRUE", True)],
)
def test_validate_inline_bools(value: str, expected: bool) -> None:
    (r,) = parse_inline(f"<!-- ruleproof: demo-all pattern=x flag={value} -->", "A.md")
    assert validate(r).params["flag"] is expected


def test_validate_inline_ints_and_lists() -> None:
    text = "<!-- ruleproof: demo-all pattern=x limit=-12 names=' a, b ,,c' patterns=p,q -->"
    params = validate(parse_inline(text, "A.md")[0]).params
    assert params["limit"] == -12
    assert params["names"] == ["a", "b", "c"]
    assert params["patterns"] == ["p", "q"]


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"id": "Bad Id"}, r"x.toml:4: rule 'Bad Id': invalid id; .* for example 'bad-id'"),
        ({"id": "-x"}, r"invalid id"),
        ({"severity": "warn"}, r"invalid severity 'warn'; expected one of error, warning, info"),
        ({"severity": "Warning"}, r"\(did you mean 'warning'\?\)"),
        (
            {"check": "demo-al"},
            r"x.toml:4: rule 'r1': unknown check 'demo-al' \(did you mean 'demo-all'\?\); "
            r"available checks: demo-all, demo-paths, demo-strict",
        ),
        (
            {"params": {"pattern": "x", "patern": "y"}},
            r"rule 'r1' \(demo-all\): unknown parameter 'patern' \(did you mean 'pattern'\?\); "
            r"demo-all accepts: flag, label, limit, names, pattern, patterns",
        ),
        ({"params": {"pattern": "x", "desciption": "y"}}, r"did you mean 'description'"),
        ({"params": {}}, r"\(demo-all\): missing required parameter pattern$"),
        ({"check": "demo-paths"}, r"missing required parameter paths\n  paths: files to protect"),
        (
            {"params": {"pattern": "x", "flag": "true"}},
            r"parameter 'flag': expected a boolean \(true or false\), found string 'true'",
        ),
        ({"params": {"pattern": "x", "limit": True}}, r"expected an integer, found boolean true"),
        ({"params": {"pattern": "x", "limit": 1.5}}, r"expected an integer, found float 1.5"),
        ({"params": {"pattern": 3}}, r"expected a regular expression string, found integer 3"),
        ({"params": {"pattern": "x", "names": ["a", 1]}}, r"expected a list of strings, found"),
        ({"params": {"pattern": "x", "label": {"a": 1}}}, r"expected a string, found table"),
        (
            {"params": {"pattern": "ab(c"}},
            r"parameter 'pattern': invalid regular expression: missing \), unterminated "
            r"subpattern at position 2\n  ab\(c\n    \^",
        ),
        ({"params": {"pattern": "x", "patterns": ["ok", "*bad"]}}, r"nothing to repeat"),
    ],
)
def test_validate_errors(kw: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        validate(rule(**kw))


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        ("flag=maybe", r"^A.md:1: rule 'a-l1' \(demo-all\): parameter 'flag': expected "),
        ("limit=1e3", r"expected a decimal integer, found '1e3'"),
    ],
)
def test_validate_inline_errors(annotation: str, message: str) -> None:
    (r,) = parse_inline(f"<!-- ruleproof: demo-all pattern=x {annotation} -->", "A.md")
    with pytest.raises(ConfigError, match=message):
        validate(r)


def test_validate_without_origin() -> None:
    with pytest.raises(ConfigError, match=r"^rule 'r1': unknown check"):
        validate(rule(origin="", check="zzz"))


def test_exclude_skips_inline_annotations(tmp_path: Path) -> None:
    (tmp_path / "ruleproof.toml").write_text('exclude = ["examples/"]\n', encoding="utf-8")
    demo = tmp_path / "examples" / "demo"
    demo.mkdir(parents=True)
    (demo / "AGENTS.md").write_text("<!-- ruleproof: no-such-check -->\n", encoding="utf-8")
    assert load_rules(tmp_path) == []


def test_exclude_must_be_list_of_strings() -> None:
    with pytest.raises(ConfigError, match="exclude must be a list"):
        parse_rules_toml("exclude = 3\n", "ruleproof.toml")


# ------------------------------------------------------------------- review regressions


@pytest.mark.parametrize(
    "text",
    [
        "Example:\n\n    <!-- ruleproof: nope x=1 -->\n",
        "Example:\n\n\t<!-- ruleproof: nope x=1 -->\n",
        "1. Example:\n\n    ```md\n    <!-- ruleproof: nope x=1 -->\n    ```\n",
        "- Item\n\n      <!-- ruleproof: nope x=1 -->\n",
        "<!-- ruleproof is configured in ruleproof.toml -->\n",
        "<!-- ruleproofing notes -->\n",
    ],
)
def test_parse_inline_ignores_indented_code_and_plain_comments(text: str) -> None:
    assert parse_inline(text, "AGENTS.md") == []


def test_parse_inline_list_continuation_is_not_code() -> None:
    text = "1. Keep it.\n\n    <!-- ruleproof: demo-paths paths=a -->\n"
    (r,) = parse_inline(text, "AGENTS.md")
    assert (r.id, r.description) == ("agents-l3", "Keep it.")
    indented = "Prose line\n    <!-- ruleproof: demo-paths paths=a -->\n"  # no blank: not code
    assert len(parse_inline(indented, "AGENTS.md")) == 1


def test_parse_inline_description_is_whole_paragraph() -> None:
    text = (
        "# Rules\n"
        "\n"
        "Intro paragraph.\n"
        "\n"
        "- Never edit `api/gen/` **by hand**;\n"
        "  regenerate it with   `make gen`.\n"
        "  <!-- ruleproof: demo-paths paths=api/gen/ -->\n"
        "- Second item\n"
        "  wraps here. <!-- ruleproof: demo-paths paths=b -->\n"
        "## Heading only\n"
        "<!-- ruleproof: demo-paths paths=c -->\n"
        "\n" + "word " * 60 + "\n<!-- ruleproof: demo-paths paths=d -->\n"
    )
    first, second, third, fourth = parse_inline(text, "AGENTS.md")
    assert first.description == "Never edit `api/gen/` by hand; regenerate it with `make gen`."
    assert second.description == "Second item wraps here."
    assert third.description == "Heading only"
    assert len(fourth.description) <= 200
    assert fourth.description.endswith("word...")


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"paths": ["x"], "mode": "fats"},
            r"parameter 'mode': invalid value 'fats' \(did you "
            r"mean 'fast'\?\); expected one of fast, slow",
        ),
        (
            {"paths": ["x"], "actions": ["add", "added"]},
            r"invalid value 'added' \(did you mean "
            r"'add'\?\); expected one of add, modify, delete",
        ),
        ({"paths": []}, r"parameter 'paths': must not be empty"),
        ({}, r"\(demo-strict\): set at least one of paths, max_files"),
        (
            {"paths": ["[z-a]"]},
            r"x.toml:4: rule 'r1' \(demo-strict\): parameter 'paths': invalid "
            r"glob pattern '\[z-a\]': reversed range z-a",
        ),
        ({"paths": ["[!]x"]}, r"unclosed character class"),
    ],
)
def test_validate_choices_nonempty_one_of_globs(params: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        validate(rule(check="demo-strict", params=params))


def test_validate_choices_and_one_of_accept_valid_values() -> None:
    out = validate(rule(check="demo-strict", params={"max_files": 3, "actions": "modify"}))
    assert out.params == {"mode": "fast", "actions": ["modify"], "paths": None, "max_files": 3}
    (r,) = parse_inline("<!-- ruleproof: demo-strict paths=a mode=slow -->", "A.md")
    assert validate(r).params["mode"] == "slow"


def test_validate_rejects_control_characters_in_regexes() -> None:
    basic = '[[rule]]\nid = "a"\ncheck = "demo-all"\npattern = "\\bfoo"\n'  # TOML: \b = BS
    (r,) = parse_rules_toml(basic, "r.toml")
    with pytest.raises(ConfigError) as exc:
        validate(r)
    assert str(exc.value).startswith(
        "r.toml:1: rule 'a' (demo-all): parameter 'pattern': contains a control character "
        "'\\x08' (\\b became a backspace?); use a TOML literal string"
    )
    with pytest.raises(ConfigError, match="control character"):
        validate(rule(params={"pattern": "ok", "patterns": ["fine", "a\x0cb"]}))
    literal = parse_rules_toml(basic.replace('"\\bfoo"', "'\\bfoo'"), "r.toml")
    assert validate(literal[0]).params["pattern"] == "\\bfoo"


def test_exclude_globs_are_validated() -> None:
    with pytest.raises(ConfigError, match=r"^ruleproof.toml:2: invalid glob pattern '\[z-a\]'"):
        parse_rules_toml('version = 1\nexclude = ["ok/", "[z-a]"]\n', "ruleproof.toml")
