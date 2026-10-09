# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - Unreleased

First release.

### Added

- `ruleproof check`: run rules against the git diff (or a patch file) and an agent session
  transcript. Text, JSON, Markdown and SARIF output. Exit codes 0 / 1 / 2, `--fail-on`,
  `--strict`.
- Rules from `ruleproof.toml`, `[tool.ruleproof]` in `pyproject.toml`, and inline
  `<!-- ruleproof: ... -->` annotations in instruction files, with `file:line` load errors.
- Checks: `forbid-change`, `require-change`, `forbid-text`, `require-text`, `max-diff`,
  `forbid-command`, `require-command`, `forbid-edit`, `forbid-tool`, `forbid-message`, `claims`.
- `claims`: flags "tests pass", "lint is clean", "committed" and similar claims with no
  successful command to back them.
- Transcript parsers for Claude Code (session JSONL and `stream-json`), Codex CLI (rollout JSONL
  and `codex exec --json`), Gemini CLI, and a generic JSONL format. Subagent transcripts are
  merged.
- `ruleproof sessions` and `ruleproof timeline`: find local agent sessions for a repo and export
  them as text, JSON or SQLite.
- `ruleproof compile`: draft rules from instruction-file prose, with a coverage report.
- `ruleproof doctor`: finds drift between agent instruction files, contradictions, instructions
  that disagree with the repo, dead references, oversized files, broken skills and MCP config
  drift.
- `ruleproof hook claude-stop`: Claude Code Stop hook that blocks finishing while rules fail,
  with loop protection.
- GitHub Action with job summary, SARIF output and transcript support; pre-commit hooks.
- No runtime dependencies; Python 3.11+ on Linux, macOS and Windows.

[Unreleased]: https://github.com/h-a-forster/ruleproof/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/h-a-forster/ruleproof/releases/tag/v0.1.0
