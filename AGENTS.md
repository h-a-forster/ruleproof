# AGENTS.md

Instructions for coding agents working on ruleproof. Human contributors: see CONTRIBUTING.md.

ruleproof checks these rules on its own pull requests. The `<!-- ruleproof: ... -->` comments
are the checks; `ruleproof.toml` holds the rest.

## Setup

- Use uv. Never `pip install` anything.
  <!-- ruleproof: forbid-command id=no-pip command="\bpip3?\s+install\b" -->
- Python 3.11+, standard library only at runtime. Do not add entries to
  `[project].dependencies`; dev tools go in the `dev` dependency group.

## Before you finish

- Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy`.
  All four must pass after your last edit.
- Report results accurately. Do not say tests pass unless you ran them after your last change.

## Code

- Match the surrounding style: type hints everywhere, `from __future__ import annotations`,
  short module docstrings, comments only where the reason is not obvious.
- No `print()` in `src/` outside `cli.py` and `hook.py`; return strings and let the CLI write them.
- Open files with `encoding="utf-8"`. Code must work on Windows, macOS and Linux.
- Parsers never raise on malformed input from agents: skip the record and add a warning to
  `Session.warnings`.
- A new check needs a `Param` schema, a `doc` paragraph, tests for pass, fail and skip, and a
  row in `docs/rules.md`.

## Tests and fixtures

- Every change under `src/` comes with tests under `tests/`.
- Fixtures must be synthetic. Never copy real transcripts, which can hold secrets and paths.
- Do not edit `tests/fixtures/` files to make a failing test pass; fix the code.

## Changes

- Add a line to `CHANGELOG.md` under `## [Unreleased]` for user-visible changes.
- Do not commit or push unless asked.
  <!-- ruleproof: forbid-command id=no-push severity=warning command="\bgit\s+push\b" -->

## Status

Updated: 2026-11-13. Open work lives in `backlog.md`.

Done:
- 0.1.0 and 0.1.1 released (GitHub only, installed from the git tag). 0.1.1 collects the
  `claims` fixes, the pressure benchmark (`pt1`) and the offline claims evaluation.
- Benchmarks `base1`, `p2`, `p3`, `pt1` and `claims-eval` are committed under `bench/results/`;
  every number in the README traces to one of them (the claims-eval "before" columns to
  `results-e33dc0f.md` and `results-rerun-beed533.md`).

Known limitations (stated in the README):
- The `claims` check does not yet work as a verifier. Against the pressure benchmark's judge
  it scored precision 0/12 and recall 0/8, and later fixes were checked only in-sample (the
  `pt1` transcripts are not kept, so it cannot be re-scored). On overclaim's held-out study it
  finds 89% of claims but catches 0 of 22 false completions; most of those had a passing test
  run, which a command-based check cannot see. On overclaim's rerun, unseen when first scored,
  it failed 22 of 97 correct runs (13 after the fixes found there).
- All benchmark grading is by ruleproof or a Claude judge; no human labels. One small repo,
  5 tasks: with tasks as the unit no arm difference is significant (exact sign-flip test).
- Gemini CLI parsing is untested on real sessions.
