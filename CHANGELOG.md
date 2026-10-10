# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `claims` no longer reads exit 0 as proof when the claimed tool's status is not the command
  line's: after `|` (`pytest | tail -3`), `;` (`pytest; git status`), `||` or a background `&`
  only the tool's own output counts, otherwise the claim is `unverified` (`set -o pipefail`
  keeps pipelines trusted). With an unknown exit code, chained output is read for the claimed
  kind only (`pytest && ruff check .` showing "All checks passed!" no longer backs a tests claim).
- Quoted program paths with spaces (`"C:\Program Files\Python311\python.exe" -m pytest`,
  `& 'C:\...\python.exe' -m pytest`) and `cmd /d /c "..."` / `/s` / `/q` are recognised as commands.
- `ruleproof compile`'s pip ban also matches `pip.exe install` and `C:\Py\Scripts\pip.exe install`.
- The pytest-cov failure signal no longer matches `--cov-fail-under=90` in printed config
  (`cat pyproject.toml; pytest`), which made a passing run read as failed.
- `claims` ignores partial and scoped reports: "15 of 16 tests pass", "the other 19 tests
  pass", "all 3 tests in test_totals.py pass". Found by the pressure benchmark's judge.

### Changed

- The action's `version` input installs that release from its git tag instead of PyPI.
- `claims` recognises Windows command lines (`.venv\Scripts\pytest.exe`,
  `.venv\Scripts\python.exe -m unittest`); backslashes in paths were dropped, so these runs
  were missed or an older failing run was cited. Codex shell calls in a session on a Windows
  drive are read as PowerShell. `claims` and `require-command` now agree on these commands.
- `claims` recognises `python -X utf8 -m unittest`, `python manage.py test`, `bazel test` and
  test scripts run by path (`./scripts/test.sh`), and takes `test_commands`, `lint_commands`,
  `type_commands`, `build_commands` and `format_commands` for project-specific commands.
- `claims` attributes the shared output of chained commands (`ruff check . && ruff format
  --check .`) to each tool by its summary line, and reports `unverified` when it cannot.
- `claims` ignores quoted text, blockquotes and prescriptions ("make sure the tests pass").
- `claims` catches "everything works" / "the fix is verified" (new `works` claim) and reports a
  full-suite claim backed only by a subset run (`pytest tests/test_app.py`) as `unverified`.
- `ruleproof compile` no longer exempts `uv pip install` from a pip ban whose prose says it
  includes `uv pip install`.

## [0.1.0] - 2026-10-10

First release.

### Added

- `ruleproof check`: run rules against the git diff (or a patch file) and an agent session
  transcript. Text, JSON, Markdown and SARIF output. Exit codes 0 / 1 / 2, `--fail-on`,
  `--strict`.
- Rules from `ruleproof.toml`, `[tool.ruleproof]` in `pyproject.toml`, and inline
  `<!-- ruleproof: ... -->` annotations in instruction files, with `file:line` load errors.
- Checks: `forbid-change`, `require-change`, `forbid-text`, `require-text`, `max-diff`,
  `forbid-command`, `require-command`, `forbid-edit`, `forbid-tool`, `forbid-message`, `claims`.
  `forbid-text` can redact matches in reports (`redact = true`), for secret-detection rules.
- `claims`: flags "tests pass", "lint is clean", "committed" and similar claims with no
  successful command to back them.
- Transcript parsers for Claude Code (session JSONL and `stream-json`), Codex CLI (rollout JSONL
  and `codex exec --json`), Gemini CLI, and a generic JSONL format. Subagent transcripts are
  merged.
- `ruleproof sessions` and `ruleproof timeline`: find local agent sessions for a repo and export
  them as text, JSON or SQLite.
- `ruleproof compile`: draft rules from instruction-file prose, with a coverage report.
  Directives that already carry an inline annotation are skipped, and a rule that another
  compiled rule covers is dropped.
- `ruleproof doctor`: finds drift between agent instruction files, contradictions, instructions
  that disagree with the repo, dead references, oversized files, broken skills and MCP config
  drift.
- `ruleproof hook claude-stop`: Claude Code Stop hook that blocks finishing while rules fail,
  with loop protection, `--rules` / `--base` options and a `RULEPROOF_HOOK_DISABLE` switch.
- `ruleproof hook claude-pretool`: Claude Code PreToolUse hook that denies a command, edit or
  tool call before it runs when it breaks an error-severity `forbid-command`, `forbid-edit`,
  `forbid-tool` or `forbid-change` rule (`--rules` / `--base` options, same disable switch).
  Editing a file that did not exist at `--base` counts as an add, so a "never edit existing
  tests" rule does not stop the agent from editing tests it just wrote.
- GitHub Action with job summary, SARIF output and transcript support; pre-commit hooks. On a
  shallow checkout the action deepens the history until the base and `HEAD` share a merge base.
- No runtime dependencies; Python 3.11+ (tested on 3.11 to 3.14) on Linux, macOS and Windows.

[Unreleased]: https://github.com/h-a-forster/ruleproof/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/h-a-forster/ruleproof/releases/tag/v0.1.0
