from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

import pytest

from ruleproof.compile import (
    CompiledRule,
    CompileResult,
    Directive,
    compile_files,
    extract_directives,
    render_summary,
    render_toml,
)
from ruleproof.errors import ConfigError
from ruleproof.models import Rule

FIXTURES = Path(__file__).parent / "fixtures" / "compile"

# The Checks table of docs/architecture.md: check -> {param: type}. Required params are listed
# separately. rules.py validates against the live registry; this keeps the compiler honest
# without importing it.
STR, BOOL, INT, LIST = str, bool, int, list
CHECKS: dict[str, dict[str, type]] = {
    "forbid-change": {"paths": LIST, "except": LIST, "actions": LIST},
    "require-change": {"if_changed": LIST, "then_changed": LIST, "except": LIST},
    "forbid-text": {"pattern": STR, "paths": LIST, "except": LIST, "ignore_case": BOOL},
    "require-text": {"pattern": STR, "paths": LIST, "new_files_only": BOOL},
    "max-diff": {"max_files": INT, "max_lines": INT, "except": LIST},
    "forbid-command": {"command": STR, "ignore_case": BOOL},
    "require-command": {
        "command": STR,
        "must_succeed": BOOL,
        "after_last_edit": BOOL,
        "when_paths": LIST,
        "edit_paths": LIST,
        "ignore_edit_paths": LIST,
    },
    "forbid-edit": {"paths": LIST, "outside_repo": BOOL},
    "forbid-tool": {"tool": STR},
    "forbid-message": {"pattern": STR, "ignore_case": BOOL},
    "claims": {"claims": LIST, "ignore_edit_paths": LIST},
}
REQUIRED: dict[str, set[str]] = {
    "forbid-change": {"paths"},
    "require-change": {"if_changed", "then_changed"},
    "forbid-text": {"pattern"},
    "require-text": {"pattern"},
    "forbid-command": {"command"},
    "require-command": {"command"},
    "forbid-tool": {"tool"},
    "forbid-message": {"pattern"},
}
RULE_FIELDS = {"id", "check", "description", "severity", "source", "scope"}
REGEX_PARAMS = {"command", "pattern", "tool"}


def compile_text(tmp_path: Path, text: str, name: str = "AGENTS.md") -> CompileResult:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return compile_files([path], tmp_path)


def rules_of(tmp_path: Path, text: str) -> list[Rule]:
    return [cr.rule for cr in compile_text(tmp_path, text).rules]


def only(tmp_path: Path, text: str, check: str) -> Rule:
    found = [r for r in rules_of(tmp_path, text) if r.check == check]
    assert len(found) == 1, found
    return found[0]


def commands(rules: list[Rule], check: str) -> list[str]:
    return [r.params["command"] for r in rules if r.check == check]


def matches(regex: str, text: str) -> bool:
    return re.search(regex, text) is not None


def assert_valid(doc: dict[str, Any]) -> None:
    """Every rule names a known check and only its parameters, with the right types."""
    assert doc["version"] == 1
    ids = [r["id"] for r in doc.get("rule", [])]
    assert len(ids) == len(set(ids))
    for rule in doc.get("rule", []):
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", rule["id"]), rule["id"]
        assert rule["check"] in CHECKS, rule
        assert rule.get("severity", "error") in ("error", "warning", "info")
        assert re.fullmatch(r".+:\d+", rule["source"])
        params = {k: v for k, v in rule.items() if k not in RULE_FIELDS}
        spec = CHECKS[rule["check"]]
        assert set(params) <= set(spec), (rule["id"], set(params) - set(spec))
        assert REQUIRED.get(rule["check"], set()) <= set(params), rule
        for key, value in params.items():
            assert isinstance(value, spec[key]), (rule["id"], key, value)
            if spec[key] is LIST:
                assert value and all(isinstance(v, str) and v for v in value)
            if key in REGEX_PARAMS:
                re.compile(value)
        if rule["check"] == "forbid-edit":
            assert "paths" in params or params.get("outside_repo") is True
        if "actions" in params:
            assert set(params["actions"]) <= {"add", "modify", "delete"}


# --------------------------------------------------------------------------- extraction


def test_extract_skips_structure_and_keeps_statements() -> None:
    text = (
        "---\ndescription: Never shown\n---\n"
        "# Never a directive\n\n"
        "- [Setup](#setup)\n"
        "<!-- Never run `x`\n-->\n"
        "| Never | `rm` |\n| --- | --- |\n\n"
        "The project uses Python.\n"
        "Run `make test` before\ncommitting. Use small commits.\n\n"
        "```bash\n# never run this\nrm -rf /\n```\n\n"
        "* Never `git push --force`\n"
        "  to shared branches.\n"
        "1. Prefer composition.\n"
    )
    ds = extract_directives(text, "AGENTS.md")
    assert [(d.line, d.text, d.kind) for d in ds] == [
        (13, "Run `make test` before committing.", "must"),
        (14, "Use small commits.", "must"),
        (21, "Never `git push --force` to shared branches.", "never"),
        (23, "Prefer composition.", "prefer"),
    ]


def test_extract_handles_crlf_emphasis_links_and_abbreviations() -> None:
    text = "- **Never** edit [the docs](docs/README.md), e.g. `docs/api.md`. Ok.\r\n"
    (d,) = extract_directives(text, "CLAUDE.md")
    assert d.text == "Never edit the docs, e.g. `docs/api.md`."
    assert d.location == "CLAUDE.md:1" and d.kind == "never"


def test_extract_appends_fence_commands_to_a_colon_sentence() -> None:
    text = "Before committing, run:\n\n```sh\n$ cargo fmt\n# comment\ncargo test\n```\n"
    (d,) = extract_directives(text, "AGENTS.md")
    assert d.text == "Before committing, run: `cargo fmt` `cargo test`"


def test_extract_reads_list_items_under_a_negative_or_done_lead() -> None:
    text = (
        "## Never\n\n- `npm install`\n- Commit secrets.\n  This explains why.\n\n"
        "## Notes\n\nBefore you open a PR:\n\n- `pnpm lint`\n- The CI is slow.\n"
    )
    ds = extract_directives(text, "AGENTS.md")
    assert [(d.text, d.kind, d.lead) for d in ds] == [
        ("`npm install`", "never", "Never"),
        ("Commit secrets.", "never", "Never"),
        ("`pnpm lint`", "must", "Before you open a PR:"),
    ]


# --------------------------------------------------------------------------- require-command


@pytest.mark.parametrize(
    "prose, ran",
    [
        ("Run `uv run pytest -q` before committing.", "python -m pytest tests"),
        ("- Always run `cargo test -p core` after making changes.", "cargo test --workspace"),
        ("Make sure `pnpm lint` passes before you open a PR.", "pnpm run lint"),
        ("Tests must pass (`npm test`).", "npm test"),
        ("Never commit without running `make check`.", "make check"),
        ("Don't forget to run `ruff format` before you finish.", "uv run ruff format ."),
        ("Before committing, run:\n\n```bash\nmypy src\n```\n", "mypy --strict src"),
        ("## Before submitting\n\n- `go vet ./...`\n", "go vet ./pkg/..."),
    ],
)
def test_require_command(tmp_path: Path, prose: str, ran: str) -> None:
    rule = only(tmp_path, prose, "require-command")
    assert rule.params["must_succeed"] is True and rule.params["after_last_edit"] is True
    assert matches(rule.params["command"], ran), (rule.params["command"], ran)
    assert rule.severity == "error"


def test_require_command_regex_is_not_too_loose(tmp_path: Path) -> None:
    rule = only(tmp_path, "Run `pnpm test` before committing.", "require-command")
    assert not matches(rule.params["command"], "pnpm test:e2e")
    assert not matches(rule.params["command"], "npm test")


def test_require_command_maps_a_language_condition_to_when_paths(tmp_path: Path) -> None:
    rule = only(
        tmp_path,
        "If you changed Rust code, make sure to run `cargo clippy` before committing.",
        "require-command",
    )
    assert rule.params["when_paths"] == ["*.rs"]


@pytest.mark.parametrize(
    "prose",
    [
        "To run the tests, use `pytest`.",
        "You can run `cargo test` if you want.",
        "There is no need to run `make lint` before committing.",
        "When adding a new lint rule, always run `cargo dev generate-all`.",
        "Run `pnpm install` before committing.",
        "If only non-Rust files changed, make sure to run `tools/lint.js` before committing.",
    ],
)
def test_require_command_ignores_how_tos_and_unmappable_conditions(
    tmp_path: Path, prose: str
) -> None:
    assert commands(rules_of(tmp_path, prose), "require-command") == []


# --------------------------------------------------------------------------- forbid-command


@pytest.mark.parametrize(
    "prose, bad, fine",
    [
        ("Never run `gh pr merge`.", "gh pr merge 12", "gh pr view 12"),
        ("Do not use `git reset --hard`.", "git reset --hard HEAD~1", "git reset --soft HEAD"),
        ("`rm -rf /` is forbidden.", "sudo rm -rf /", "rm -rf build"),
        ("Don't run `nvm` or `fnm`.", "nvm use 20", "node -v"),
        ("Never use `bun test` directly - use `bun bd test`.", "bun test a.ts", "bun bd test"),
        ("Never force-push to main.", "git push -f origin main", "git push --force-with-lease"),
        ("Never bypass the hooks (`--no-verify`).", "git commit --no-verify -m x", "git commit"),
    ],
)
def test_forbid_command(tmp_path: Path, prose: str, bad: str, fine: str) -> None:
    (regex,) = commands(rules_of(tmp_path, prose), "forbid-command")[:1]
    assert matches(regex, bad) and not matches(regex, fine), regex


@pytest.mark.parametrize(
    "prose",
    [
        "Do not use `curl` to test HMR issues.",
        "Never use `flox activate` in interactive sessions.",
        "Avoid `cargo test`; run `cargo test -p widget` instead.",
        "Don't forget to run `cargo fmt`.",
        "A fresh worktree has no `node_modules`.",
    ],
)
def test_forbid_command_skips_qualified_or_positive_mentions(tmp_path: Path, prose: str) -> None:
    assert commands(rules_of(tmp_path, prose), "forbid-command") == []


def test_soft_negation_is_a_warning(tmp_path: Path) -> None:
    rule = only(tmp_path, "Avoid `make test-all`.", "forbid-command")
    assert rule.severity == "warning"


# --------------------------------------------------------------------------- substitution


def test_use_uv_not_pip(tmp_path: Path) -> None:
    (regex,) = commands(rules_of(tmp_path, "Always use `uv`, not pip."), "forbid-command")
    assert matches(regex, "pip install requests")
    assert matches(regex, "python -m pip install requests")
    assert not matches(regex, "uv pip install requests")
    assert not matches(regex, "uv add requests")


def test_use_pnpm_never_npm_or_yarn(tmp_path: Path) -> None:
    regexes = commands(rules_of(tmp_path, "Use pnpm, never npm or yarn."), "forbid-command")
    assert len(regexes) == 2
    for regex in regexes:
        assert not matches(regex, "pnpm install")
    assert any(matches(r, "npm install left-pad") for r in regexes)
    assert any(matches(r, "cd web && yarn add x") for r in regexes)


def test_prefer_is_a_warning(tmp_path: Path) -> None:
    rule = only(tmp_path, "Prefer `bun` over `npm`.", "forbid-command")
    assert rule.severity == "warning" and matches(rule.params["command"], "npm run build")


def test_one_subcommand_does_not_ban_the_whole_tool(tmp_path: Path) -> None:
    rules = rules_of(tmp_path, "Never use `bun test` directly - it skips your build.")
    assert commands(rules, "forbid-command") == [r"\bbun\s+test\b"]


def test_runner_substitution_forbids_only_the_bare_command(tmp_path: Path) -> None:
    rule = only(tmp_path, "Use `uv run pytest` instead of `pytest`.", "forbid-command")
    assert matches(rule.params["command"], "pytest -x")
    assert not matches(rule.params["command"], "uv run pytest -x")


@pytest.mark.parametrize("prose", ["Use pytest patterns, not `unittest.TestCase`.", "Use uv."])
def test_substitution_needs_two_tools(tmp_path: Path, prose: str) -> None:
    assert rules_of(tmp_path, prose) == []


# --------------------------------------------------------------------------- paths


def test_never_edit_a_directory(tmp_path: Path) -> None:
    rule = only(tmp_path, "Never modify files in `migrations`.", "forbid-change")
    assert rule.params == {"paths": ["migrations/"], "actions": ["modify"]}


def test_generated_or_manual_edits_become_forbid_edit(tmp_path: Path) -> None:
    rules = rules_of(
        tmp_path,
        "- `api/gen/` is generated; do not edit it.\n"
        "- Never hand-edit generated files: `**/*.gen.ts`, `./schema.json`.\n"
        "- Do not manually edit lock files.\n",
    )
    edits = [r.params["paths"] for r in rules if r.check == "forbid-edit"]
    assert edits[0] == ["api/gen/"]
    assert edits[1] == ["**/*.gen.ts", "schema.json"]
    assert "uv.lock" in edits[2] and "package-lock.json" in edits[2]


def test_existing_tests_are_protected_with_a_warning(tmp_path: Path) -> None:
    rule = only(tmp_path, "Don't modify existing tests to make them pass.", "forbid-change")
    assert rule.params["actions"] == ["modify", "delete"] and rule.severity == "warning"


@pytest.mark.parametrize(
    "prose",
    ["Never modify the `gh/user/1` branches.", "Never commit `.stats.yml` from a branch."],
)
def test_paths_that_are_not_files_are_ignored(tmp_path: Path, prose: str) -> None:
    assert [r for r in rules_of(tmp_path, prose) if r.check.startswith("forbid-")] == []


def test_backup_files(tmp_path: Path) -> None:
    rule = only(tmp_path, "Don't create backup files (`.bak`, `*.before_*`).", "forbid-change")
    assert rule.params == {"paths": ["*.bak", "*.orig", "*.before_*"], "actions": ["add"]}


def test_outside_repo(tmp_path: Path) -> None:
    rule = only(tmp_path, "Do not touch files outside the repository.", "forbid-edit")
    assert rule.params == {"outside_repo": True}


# --------------------------------------------------------------------------- require-change


def test_changelog_uses_the_repo_layout(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "CHANGES.md").write_text("", encoding="utf-8")
    rule = only(tmp_path, "Update the changelog for every user-facing change.", "require-change")
    assert rule.params["if_changed"] == ["src/**"]
    assert rule.params["then_changed"] == ["CHANGES.md"]
    assert rule.severity == "warning"


def test_tests_and_changesets(tmp_path: Path) -> None:
    rules = rules_of(tmp_path, "- Add tests for new code.\n- Always add a changeset.\n")
    targets = [r.params["then_changed"] for r in rules if r.check == "require-change"]
    assert "**/tests/**" in targets[0] and targets[1] == [".changeset/*.md"]
    assert all(r.severity == "warning" for r in rules)


@pytest.mark.parametrize(
    "prose", ["Create tests in the `test/` folder.", "Don't update the changelog for typos."]
)
def test_require_change_needs_an_obligation(tmp_path: Path, prose: str) -> None:
    assert [r for r in rules_of(tmp_path, prose) if r.check == "require-change"] == []


# --------------------------------------------------------------------------- forbid-text


@pytest.mark.parametrize(
    "prose, added, glob",
    [
        ("Don't leave `console.log` in the code.", "  console.log(x)", "*.ts"),
        ("Remove `breakpoint()` calls before committing.", "breakpoint()", "*.py"),
        ("No `# type: ignore` comments.", "x = f()  # type: ignore", "*.py"),
        ("Never use `any` in TypeScript.", "let x: any = 1", "*.ts"),
        ("Never by adding `# noqa: S101`.", "assert x  # noqa: S101", "*.py"),
        ("Do not use `datetime.now()`.", "t = datetime.now()", "*.py"),
        ("Never commit private keys or `.env` files.", "-----BEGIN PRIVATE KEY-----", ""),
    ],
)
def test_forbid_text(tmp_path: Path, prose: str, added: str, glob: str) -> None:
    rules = [r for r in rules_of(tmp_path, prose) if r.check == "forbid-text"]
    assert any(matches(r.params["pattern"], added) for r in rules), rules
    if glob:
        assert all(glob in r.params["paths"] for r in rules)


def test_specific_marker_does_not_forbid_the_general_one(tmp_path: Path) -> None:
    rule = only(tmp_path, "Never fix S101 by adding `# noqa: S101`.", "forbid-text")
    assert not matches(rule.params["pattern"], "import x  # noqa: F401")


@pytest.mark.parametrize(
    "prose",
    [
        "Use `# type: ignore[code]`, not bare `# type: ignore`.",
        "Avoid `Any` in Python signatures.",
        "Don't use `object`.",
        "In `core/`, do not call `session.commit()`.",
        "If B950 triggers, you cannot fix it with `# noqa: B950`.",
    ],
)
def test_forbid_text_skips_ambiguous_mentions(tmp_path: Path, prose: str) -> None:
    assert [r for r in rules_of(tmp_path, prose) if r.check == "forbid-text"] == []


# --------------------------------------------------------------------------- commit, deps, claims


def test_never_commit_or_push(tmp_path: Path) -> None:
    rules = rules_of(tmp_path, "Never commit or push your changes.")
    assert sorted(commands(rules, "forbid-command")) == [r"\bgit\s+commit\b", r"\bgit\s+push\b"]
    assert all(r.severity == "error" for r in rules)


def test_commit_unless_asked_is_a_warning(tmp_path: Path) -> None:
    rule = only(tmp_path, "Don't commit unless the user asks you to.", "forbid-command")
    assert rule.severity == "warning"


@pytest.mark.parametrize(
    "prose",
    ["Never commit secrets.", "Never commit without running `make check`.", "You cannot push."],
)
def test_commit_with_an_object_is_not_a_commit_ban(tmp_path: Path, prose: str) -> None:
    assert r"\bgit\s+commit\b" not in commands(rules_of(tmp_path, prose), "forbid-command")


def test_dependencies_use_the_repo_manifests(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    rule = only(tmp_path, "Never add new dependencies without asking.", "forbid-change")
    assert rule.params["paths"] == ["pyproject.toml"] and rule.severity == "warning"


def test_claims(tmp_path: Path) -> None:
    rule = only(tmp_path, "Never claim that the tests pass without running them.", "claims")
    assert rule.params == {}


def test_license_header(tmp_path: Path) -> None:
    rule = only(tmp_path, "Every new file must start with the SPDX license header.", "require-text")
    assert rule.params["pattern"] == "SPDX-License-Identifier"
    assert rule.params["new_files_only"] is True


# --------------------------------------------------------------------------- compile_files


def test_compile_dedupes_across_files_and_counts_coverage(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "- Never run `git push --force`.\n- Prefer small functions.\n", encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text("Never force-push.\n", encoding="utf-8")
    result = compile_files([tmp_path / "AGENTS.md", tmp_path / "CLAUDE.md"], tmp_path)
    assert result.files == ["AGENTS.md", "CLAUDE.md"]
    assert [cr.rule.id for cr in result.rules] == ["no-force-push"]
    assert [d.text for d in result.uncovered] == ["Prefer small functions."]
    assert result.coverage == pytest.approx(2 / 3)


def test_nested_instruction_files_get_a_scope(tmp_path: Path) -> None:
    result = compile_text(tmp_path, "Never edit `gen/` by hand.\n", "pkg/web/AGENTS.md")
    (cr,) = result.rules
    assert cr.rule.scope == "pkg/web" and cr.rule.source == "pkg/web/AGENTS.md:1"


def test_ids_are_unique(tmp_path: Path) -> None:
    result = compile_text(
        tmp_path, "- Never run `make deploy`.\n\n## Never\n\n- `make deploy --prod`\n"
    )
    assert [cr.rule.id for cr in result.rules] == ["no-make-deploy", "no-make-deploy-prod"]
    result = compile_text(tmp_path, "- Never edit `a/b/`.\n- Never edit `a-b/`.\n", "x/AGENTS.md")
    assert [cr.rule.id for cr in result.rules] == ["no-change-a-b", "no-change-a-b-2"]


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        compile_files([tmp_path / "AGENTS.md"], tmp_path)


def test_empty_input() -> None:
    result = CompileResult()
    assert result.coverage == 0.0
    assert tomllib.loads(render_toml(result)) == {"version": 1}
    assert render_summary(result).startswith("0 directives in 0 files")


# --------------------------------------------------------------------------- rendering


def test_fixture_renders_valid_rules(tmp_path: Path) -> None:
    shutil.copy(FIXTURES / "AGENTS.md", tmp_path / "AGENTS.md")
    (tmp_path / "src").mkdir()
    result = compile_files([tmp_path / "AGENTS.md"], tmp_path)
    text = render_toml(result)
    doc = tomllib.loads(text)
    assert_valid(doc)
    assert text == render_toml(compile_files([tmp_path / "AGENTS.md"], tmp_path))

    ids = {r["id"] for r in doc["rule"]}
    assert {
        "no-pip",
        "run-pytest",
        "run-ruff-check",
        "no-force-push",
        "no-verify",
        "no-edit-src-widget-generated",
        "no-backup-files",
        "update-changelog",
        "no-breakpoint",
        "no-git-commit",
        "no-edit-outside-repo",
        "no-new-dependencies",
        "no-npm-install",
        "no-secret-files",
        "run-cargo-clippy",
    } <= ids
    assert text.startswith("# Generated by `ruleproof compile`")
    assert "# AGENTS.md:27  Never run `git push --force`.\n[[rule]]" in text
    assert '# AGENTS.md:46  `npm install`  (under "Never")' in text
    assert "# AGENTS.md:36  Prefer small functions." in text.split("Not compiled")[1]
    assert "make test" not in text  # tables and example fences are not rules


def test_render_escapes_strings(tmp_path: Path) -> None:
    d = Directive("AGENTS.md", 3, "Don't run `it's \"odd\"`\x01", "never", lead="Never")
    rule = Rule(
        id="odd",
        check="forbid-command",
        params={"command": r"it's \"odd\"\b", "ignore_case": True},
        description='Quote " and backslash \\ and tab\t',
        source="AGENTS.md:3",
    )
    result = CompileResult(["AGENTS.md"], [d], [CompiledRule(rule, d, 0.9, "never-run")], [])
    doc = tomllib.loads(render_toml(result))
    (got,) = doc["rule"]
    assert got["command"] == r"it's \"odd\"\b"
    assert got["description"] == 'Quote " and backslash \\ and tab\t'
    assert got["ignore_case"] is True
    assert_valid(doc)


def test_regexes_are_literal_strings_when_possible(tmp_path: Path) -> None:
    text = render_toml(compile_text(tmp_path, "Never run `git clean -fdx`.\n"))
    assert r"command = '\bgit\s+clean\s+-fdx\b'" in text


def test_summary(tmp_path: Path) -> None:
    result = compile_text(
        tmp_path, "- Never run `make deploy`.\n- Avoid `make slow`.\n- Prefer small functions.\n"
    )
    summary = render_summary(result)
    assert summary.splitlines()[0] == (
        "3 directives in 1 file; 2 compiled into 2 rules (67% coverage)."
    )
    assert "forbid-command 2" in summary and "1 rule is a warning" in summary
    assert "AGENTS.md:3  Prefer small functions." in summary


# --------------------------------------------------------------------------- review regressions


@pytest.mark.parametrize(
    "prose",
    [
        "Never edit `CHANGELOG.md` except under `## [Unreleased]`.",
        "Never run `npm install` when the lockfile is missing.",
        "Never use `pip install` other than in CI.",
        "Don't run `make deploy`, but it's fine for staging.",
        "Never add `console.log` unless you are debugging locally.",
        "Do not touch `vendor/` apart from security patches.",
        "Avoid `cargo test` only for slow suites.",
        "Never run `git reset --hard` if you have uncommitted work.",
    ],
)
def test_exceptions_and_conditions_we_cannot_express_compile_to_nothing(
    tmp_path: Path, prose: str
) -> None:
    result = compile_text(tmp_path, prose)
    assert result.rules == [] and len(result.uncovered) == 1


def test_except_to_add_keeps_only_modify_and_delete(tmp_path: Path) -> None:
    rule = only(
        tmp_path, "Don't modify `tests/fixtures/` except to add new fixtures.", "forbid-change"
    )
    assert rule.params == {"paths": ["tests/fixtures/"], "actions": ["modify", "delete"]}
    assert rule.severity == "error"


@pytest.mark.parametrize(
    "prose",
    [
        "Never add `console.log` to the code; it's fine in `scripts/`.",
        "Don't leave `console.log` except in `scripts/`.",
        "No `console.log` outside `scripts/`.",
    ],
)
def test_path_exceptions_become_except(tmp_path: Path, prose: str) -> None:
    rule = only(tmp_path, prose, "forbid-text")
    assert rule.params["except"] == ["scripts/"]


def test_forbid_text_scope_and_exception(tmp_path: Path) -> None:
    rule = only(tmp_path, "No `print()` in `src/` outside `cli.py` and `hook.py`.", "forbid-text")
    assert rule.params["paths"] == ["src/**/*.py", "src/**/*.pyi"]
    assert rule.params["except"] == ["cli.py", "hook.py"]


def test_secret_file_exceptions_are_kept(tmp_path: Path) -> None:
    rule = only(tmp_path, "Never commit secrets (except `.env.local.example`).", "forbid-change")
    assert ".env.local.example" in rule.params["except"]


def test_claims_unless_you_ran_them_is_what_the_check_does(tmp_path: Path) -> None:
    prose = "Do not say tests pass unless you ran them after your last change."
    assert only(tmp_path, prose, "claims").severity == "error"


def test_unless_asked_makes_any_prohibition_a_warning(tmp_path: Path) -> None:
    rule = only(tmp_path, "Never run `git reset --hard` unless asked.", "forbid-command")
    assert rule.severity == "warning"


def test_literal_command_ending_in_an_argument_is_anchored(tmp_path: Path) -> None:
    (regex,) = commands(rules_of(tmp_path, "Never run `rm -rf /`."), "forbid-command")
    assert matches(regex, "rm -rf /") and matches(regex, "sudo rm -rf / && ls")
    assert not matches(regex, "rm -rf /tmp/build")
    (flag,) = commands(rules_of(tmp_path, "Never run `git clean -fdx`."), "forbid-command")
    assert matches(flag, "git clean -fdx build/")


def test_require_command_skips_qa_and_docs_only_sessions(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    rule = only(tmp_path, "Run `uv run pytest` before finishing.", "require-command")
    assert rule.params["when_paths"] == ["*.py"]
    assert rule.params["ignore_edit_paths"] == ["*.md", "*.rst", "docs/**"]


def test_require_command_unless_only_docs_changed(tmp_path: Path) -> None:
    prose = "Run `pytest` before committing, unless you only changed docs."
    rule = only(tmp_path, prose, "require-command")
    assert "*.md" in rule.params["ignore_edit_paths"]


@pytest.mark.parametrize(
    "prose, actions, severity",
    [
        (
            "Do not edit `tests/fixtures/` files to make a failing test pass; fix the code.",
            ["modify", "delete"],
            "warning",
        ),
        ("Never edit `api/`.", ["modify", "delete"], "error"),
        ("Never touch `vendor/`.", ["modify", "delete"], "error"),
        ("Never delete files in `migrations/`.", ["delete"], "error"),
        ("Never create files in `legacy/`.", ["add"], "error"),
    ],
)
def test_edit_verbs_map_to_actions(
    tmp_path: Path, prose: str, actions: list[str], severity: str
) -> None:
    rule = only(tmp_path, prose, "forbid-change")
    assert rule.params["actions"] == actions and rule.severity == severity
