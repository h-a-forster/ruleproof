# Rules reference

A rule turns one line of an instruction file into a check. ruleproof runs each rule against the
evidence it has: the **diff** (what changed in the repo) and the **transcript** (what the agent
did and said). No model is called; the same inputs always give the same result.

Rules come from two places, used together:

- **one rules file**: the first that exists of `ruleproof.toml`, `.ruleproof.toml` and
  `pyproject.toml` with a `[tool.ruleproof]` table (same schema), all at the repo root. Rules
  files are never merged: if you have more than one, only the first is read;
- inline annotations in instruction files (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, ...).

`ruleproof check --rules FILE` reads that file instead of looking for one. A rule id may be
defined only once across the rules file and all annotations; a duplicate is a load error that
cites both places.
`ruleproof compile` drafts a `ruleproof.toml` from the prose in your instruction files.

## The rules file

```toml
version = 1
exclude = ["examples/", "tests/fixtures/"]  # optional

[[rule]]
id = "no-backup-files"
description = "Don't create .bak / .orig copies; use git."
source = "AGENTS.md:14"
check = "forbid-change"
actions = ["add"]
paths = ["*.bak", "*.orig", "*.before_*"]
```

Rule fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique name, shown in reports. |
| `check` | yes | One of the checks below. `ruleproof checks` lists them. |
| `description` | no | The instruction in words. Shown when the rule fails. |
| `severity` | no | `error` (default), `warning` or `info`. Compared with `--fail-on`. |
| `source` | no | Where the prose rule lives, e.g. `AGENTS.md:14`. Shown in reports. |
| `scope` | no | Directory prefix. Diff checks only see files under it. |

Top-level keys: `version` (only `1`; optional), `exclude` (globs of files and directories
that are not scanned for inline annotations, e.g. example projects and test fixtures that
contain their own `AGENTS.md`) and the `[[rule]]` tables.

Every other key in a rule is a parameter of the check. Rules are validated when they are
loaded, never silently skipped later. A load error cites `file:line`, says what was expected
and what was found, suggests the likely name for typos, and makes `ruleproof` exit with code 2.
Load errors include:

- an unknown check or parameter, a missing required parameter, a value of the wrong type;
- a value that is not one of the allowed choices (e.g. `actions = ["added"]`; the allowed
  values are listed per check below and by `ruleproof checks`);
- an empty list where at least one value is needed, or none of a check's "set at least one
  of" parameters set (e.g. `max-diff` without `max_files` or `max_lines`);
- an invalid regex, an invalid glob (e.g. `[z-a]`, an unclosed `[`), or a regex that contains
  a control character because a TOML basic string turned `"\b"` into a backspace.

Regexes are Python `re` patterns, matched with `search` (anywhere in the text). In TOML, write
them as literal strings (`'\bpytest\b'`) so backslashes need no escaping. Use `'''...'''` when
the pattern contains a single quote.

## Inline annotations

Put a rule next to the prose it enforces. It is an HTML comment, so it does not show in rendered
Markdown:

```markdown
- Never edit generated code in `api/gen/`.
  <!-- ruleproof: forbid-change paths=api/gen/ -->
- Never run `pip install`; use `uv add`.
  <!-- ruleproof: forbid-command id=no-pip command="\bpip3?(?:\.exe)? install\b" -->
```

Syntax: `<!-- ruleproof: <check> key=value key="value with spaces" -->`.

- Quote values that contain spaces.
- List values are comma-separated: `paths=api/gen/,proto/*.pb.go`.
- `id`, `severity` and `description` are optional.
  - `id` defaults to the lowercased `<file-stem>-l<line>`, e.g. `agents-l18`; in a nested
    file the directory is a prefix: `pkg/agents-l7`.
  - `description` defaults to the paragraph or list item just above the annotation (or the
    prose before it on the same line), without Markdown markup, capped at 200 characters.
  - `source` is always the annotation's file and line.
- An annotation in a nested instruction file (`pkg/AGENTS.md`) gets `scope = "pkg"`: its diff
  checks only see files under `pkg/`.
- Values are text: booleans are `true`/`false`/`yes`/`no`/`1`/`0`, integers are decimal.
- The comment must start with `ruleproof:` (a comment like `<!-- ruleproof is configured in
  ruleproof.toml -->` is ignored), and annotations inside code (fenced or indented code blocks
  and inline code spans) are ignored, so documentation can show examples.

## Globs

Path parameters (`paths`, `except`, `if_changed`, ...) take gitignore-style globs:

- Paths are repo-relative and use `/`, on every OS: `src/app.py`.
- A pattern matches a path and everything below it: `api/gen` matches `api/gen/x.go`.
- A pattern without `/` (a trailing `/` does not count) matches at any depth: `*.bak` matches
  `a/b/x.bak`; `examples` and `examples/` match `examples/demo/AGENTS.md` and
  `pkg/examples/x.py`.
- A pattern with `/` at the start or in the middle is anchored at the repo root: `src/*.py`
  matches `src/a.py`, not `src/sub/a.py` or `lib/src/a.py`.
- A leading `/` anchors a pattern at the root: `/pyproject.toml` matches only the root one.
- `**` matches zero or more whole directories: `src/**/*.py` matches `src/a.py` and
  `src/x/y/a.py`.
- `*` and `?` never cross `/`.
- `[abc]`, `[a-z]` and `[!abc]` are character classes. To match a literal `*`, `?` or `[`, put
  it in a class: `[*]`, `[?]`, `[[]`. Backslash is not an escape (it is a Windows path
  separator). An unclosed `[` or a reversed range is a load error.
- Matching is case-sensitive.

Transcript paths are often absolute (`C:\work\repo\src\app.py`, `/c/work/repo/src/app.py`,
`/home/me/repo/src/app.py`). ruleproof maps them to repo-relative paths before matching. Paths
outside the repo never match a glob; use `forbid-edit` with `outside_repo = true` for those.

## Results

Each rule ends in one of four states:

| Status | Meaning |
| --- | --- |
| `pass` | The evidence shows the rule held. |
| `fail` | The evidence shows the rule was broken. The report says where. |
| `skip` | An input the check needs is missing, e.g. a transcript rule run with `--no-transcript`. |
| `unverified` | The evidence exists but cannot settle it, e.g. the test command ran but the transcript has no exit code. |

`ruleproof check` exits with `1` when a rule fails at or above `--fail-on` (default `error`).
`skip` and `unverified` never fail the run, except that `--strict` counts `unverified` as a
failure. Use `--strict` when your transcripts always record exit codes and you want "could not
confirm" to block.

## Checks

| Check | Needs |
| --- | --- |
| [`forbid-change`](#forbid-change) | diff or transcript |
| [`require-change`](#require-change) | diff |
| [`forbid-text`](#forbid-text) | diff |
| [`require-text`](#require-text) | diff |
| [`max-diff`](#max-diff) | diff |
| [`forbid-command`](#forbid-command) | transcript |
| [`require-command`](#require-command) | transcript |
| [`forbid-edit`](#forbid-edit) | transcript |
| [`forbid-tool`](#forbid-tool) | transcript |
| [`forbid-message`](#forbid-message) | transcript |
| [`claims`](#claims) | transcript |

### forbid-change

Fails when a changed file (in the diff) or a file the agent edited (in the transcript) matches.
Runs with either input.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `paths` | globs | required | Files that must not change. |
| `except` | globs | `[]` | Files exempt from `paths`. |
| `actions` | list | all | Which changes count: `add`, `modify`, `delete`. |
| `include_ignored` | bool | `false` | Also count agent edits to gitignored files (never in the diff). |

```toml
[[rule]]
id = "migrations-append-only"
description = "Never edit an applied migration; add a new one."
check = "forbid-change"
paths = ["migrations/versions/*.py"]
actions = ["modify", "delete"]
```

### require-change

Fails when some changed file matches `if_changed` but none matches `then_changed`.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `if_changed` | globs | required | Files whose change triggers the rule. |
| `then_changed` | globs | required | At least one of these must change too. |
| `except` | globs | `[]` | Files that do not trigger the rule. |

```toml
[[rule]]
id = "models-need-migration"
check = "require-change"
if_changed = ["orders/models/**/*.py"]
then_changed = ["migrations/versions/*.py"]
except = ["orders/models/__init__.py"]
```

### forbid-text

Fails when an added line matches `pattern`. Removed and unchanged lines are ignored, so existing
code does not fail the rule. Rules files and `<!-- ruleproof: -->` annotations are skipped, so a
rule never matches its own definition.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `pattern` | regex | required | Text that must not be added. |
| `paths` | globs | all files | Files to look in. |
| `except` | globs | `[]` | Files to skip. |
| `ignore_case` | bool | `false` | Case-insensitive match. |
| `redact` | bool | `false` | Hide the matched text in evidence (for secrets): `<redacted:N chars>`. |

Evidence never shows more than the first 4 characters of strings that look like API tokens
(`sk-`, `ghp_`, `github_pat_`, `AKIA`, `AIza`, `glpat-`, ...), with or without `redact`.

```toml
[[rule]]
id = "no-print"
description = "Use logging, not print, in library code."
check = "forbid-text"
pattern = '^\s*print\('
paths = ["orders/**/*.py"]
except = ["orders/cli.py"]
```

### require-text

Fails when a file in scope has no match. Added files are checked against their content; with
`new_files_only = false`, modified files are checked against their full content.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `pattern` | regex | required | Text each file must contain. |
| `paths` | globs | all files | Files to check. |
| `new_files_only` | bool | `true` | Only check added files. |

```toml
[[rule]]
id = "license-header"
check = "require-text"
pattern = 'SPDX-License-Identifier: Apache-2\.0'
paths = ["src/**/*.py"]
```

### max-diff

Fails when the diff is larger than the limits. Set at least one limit.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `max_files` | int | none | Most changed files allowed. |
| `max_lines` | int | none | Most added + removed lines allowed. |
| `except` | globs | `[]` | Files not counted (lock files, generated code). |

```toml
[[rule]]
id = "small-changes"
severity = "warning"
check = "max-diff"
max_lines = 400
except = ["uv.lock", "package-lock.json"]
```

### forbid-command

Fails when the agent ran a matching shell command. The command line is unwrapped first, so
`bash -lc "pip install x"` is matched as `pip install x`.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `command` | regex | required | Commands that must not run. |
| `ignore_case` | bool | `false` | Case-insensitive match. |
| `match_quoted` | bool | `false` | Also match inside quoted strings and heredocs (commit messages, `echo` text). |

```toml
[[rule]]
id = "no-skipped-hooks"
check = "forbid-command"
command = '\bgit\s+(commit|push)\b.*\s--no-verify\b'
```

### require-command

Fails when no matching command ran. By default the command must come after the agent's last
counted edit and exit 0. If it ran but the transcript has no exit code, the result is
`unverified`.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `command` | regex | required | The command that must run. |
| `must_succeed` | bool | `true` | Require exit code 0. |
| `after_last_edit` | bool | `true` | Only count runs after the last counted edit. |
| `when_paths` | globs | always | Apply the rule only when a changed (diff) or edited (transcript) file matches; otherwise it passes. |
| `edit_paths` | globs | all | Edits that count for `after_last_edit`. |
| `ignore_edit_paths` | globs | `[]` | Edits that do not count (docs, notes). |
| `match_quoted` | bool | `false` | Also match inside quoted strings and heredocs (commit messages, `echo` text). |

```toml
[[rule]]
id = "tests-before-done"
check = "require-command"
command = '\bpytest\b'
when_paths = ["src/**", "tests/**"]
ignore_edit_paths = ["**/*.md"]
```

### forbid-edit

Fails when the agent edited a matching path through a tool, even if it reverted the change later
(so the diff is clean). With `outside_repo = true`, any edit outside the repo fails.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `paths` | globs | none | Paths the agent must not edit. |
| `outside_repo` | bool | `false` | Fail on edits outside the repo. |

Set `paths`, `outside_repo`, or both.

```toml
[[rule]]
id = "stay-in-repo"
check = "forbid-edit"
outside_repo = true
```

### forbid-tool

Fails when the agent called a matching tool. Tool names are the agent's own: `WebFetch`,
`Bash`, `mcp__github__create_issue` (Claude Code); `exec_command`, `apply_patch` (Codex);
`web_fetch`, `run_shell_command` (Gemini CLI).

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tool` | regex | required | Tool names that must not be called. |

```toml
[[rule]]
id = "no-web"
check = "forbid-tool"
tool = '^(WebFetch|WebSearch|web_fetch|google_web_search)$'
```

### forbid-message

Fails when an assistant message matches.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `pattern` | regex | required | Text the agent must not say. |
| `ignore_case` | bool | `false` | Case-insensitive match. |
| `subagents` | bool | `false` | Also check subagents' messages; by default only the main agent's. |

```toml
[[rule]]
id = "no-dismissed-failures"
severity = "warning"
check = "forbid-message"
pattern = '\b(pre-?existing|unrelated)\s+(test\s+)?failures?\b'
ignore_case = true
```

### claims

Checks what the agent says it did against what it ran. It reads the assistant messages after the
last counted edit (the agent's report), finds claims, and ignores negated, hedged and
prescriptive sentences ("I couldn't run the tests", "make sure the tests pass") as well as quoted
text and blockquotes (an agent quoting a rule is not reporting a result). For each claim it looks
for an evidence command after the last counted edit. The exit code backs a claim only when it is the tool's own: in
`pytest | tail -3`, `pytest; git status` or `pytest || true` it belongs to another command,
so only the tool's own summary line in the output (`16 passed`, `1 failed`) decides, and the claim
is unverified without one.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `claims` | list | all | Which claims to check (table below). |
| `ignore_edit_paths` | globs | `["*.md", "*.rst", "*.txt"]` | Edits that do not count as "last edit". |
| `test_commands` | regexes | `[]` | The project's own test commands (`make ci`, `./tools/check`), besides the built-in runners. Backs `tests` and `works`. |
| `lint_commands` | regexes | `[]` | The project's own lint commands. |
| `type_commands` | regexes | `[]` | The project's own type-check commands. |
| `build_commands` | regexes | `[]` | The project's own build commands. |
| `format_commands` | regexes | `[]` | The project's own formatter commands. |

Built-in tools must be in command position: `uv run pytest`, `python -X utf8 -m unittest`,
`.venv\Scripts\pytest.exe` and `./scripts/test.sh` count, `cat pytest.ini` does not. The
`*_commands` patterns are searched anywhere in the command line, like `require-command`'s
`command`, so a claim and a `require-command` rule with the same pattern agree.

| Claim | Example phrases | Evidence |
| --- | --- | --- |
| `tests` | "all tests pass", "42 passed" | pytest, unittest, jest, vitest, go test, cargo test, bazel test, `manage.py test`, `npm test`, test scripts (`./scripts/test.sh`), ... |
| `lint` | "lint is clean", "ruff passes" | ruff, flake8, eslint, golangci-lint, clippy, ... |
| `types` | "type-checks cleanly", "mypy passes" | mypy, pyright, tsc, ... |
| `build` | "the build succeeds", "it compiles" | build commands |
| `format` | "formatted with black" | formatters |
| `commit` | "I committed" | `git commit` |
| `push` | "pushed to origin" | `git push` |
| `works` | "everything works", "the fix is verified" | the test commands |

Outcomes per claim: no evidence command fails ("claimed but not done"); an evidence command with
a non-zero exit fails ("claimed but failing"); an evidence command with an unknown exit is
`unverified`.

Chained commands share one exit code and one output. When the evidence tool is chained with
other commands (`ruff check . && ruff format --check .`; `cd`, `echo`, `tail` and similar do not
count), the output is read for the claimed tool's own summary line: ruff's "All checks passed!"
backs "lint is clean" even though the format check failed, and "Would reformat" contradicts
"code is formatted". When the output has no summary line for the claimed tool the claim is
`unverified`, not failed.

A full-suite test claim ("all 120 tests pass", "the full suite is green") backed only by a run
that selects tests (`pytest tests/test_app.py`, `pytest -k parser`, `--lf`,
`python -m unittest tests.test_app`) or whose output reports fewer tests than claimed is
`unverified` (partial verification). "All 3 new tests pass" after `pytest tests/test_new.py` is
not a full-suite claim.

```toml
[[rule]]
id = "honest-report"
check = "claims"
claims = ["tests", "lint", "types"]
ignore_edit_paths = ["**/*.md"]
```

## A full example

[`examples/python-service/`](../examples/python-service/) has an `AGENTS.md` with inline
annotations and a `ruleproof.toml` that uses every check.
