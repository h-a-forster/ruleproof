# Rules reference

A rule turns one line of an instruction file into a check. ruleproof runs each rule against the
evidence it has: the **diff** (what changed in the repo) and the **transcript** (what the agent
did and said). No model is called; the same inputs always give the same result.

Rules come from three places, merged:

- `ruleproof.toml` at the repo root;
- `[tool.ruleproof]` in `pyproject.toml` (same schema);
- inline annotations in instruction files (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, ...).

`ruleproof check --rules FILE` reads that file instead of `ruleproof.toml` / `pyproject.toml`.
`ruleproof compile` drafts a `ruleproof.toml` from the prose in your instruction files.

## The rules file

```toml
version = 1

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

Every other key is a parameter of the check. An unknown check, an unknown parameter, a missing
required parameter or a value of the wrong type is a load error that cites `file:line`, and
`ruleproof` exits with code 2.

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
  <!-- ruleproof: forbid-command id=no-pip command="\bpip3? install\b" -->
```

Syntax: `<!-- ruleproof: <check> key=value key="value with spaces" -->`.

- Quote values that contain spaces.
- List values are comma-separated: `paths=api/gen/,proto/*.pb.go`.
- `id`, `severity` and `description` are optional.
  - `id` defaults to `<file-stem>-L<line>`, e.g. `AGENTS-L18`.
  - `description` defaults to the nearest prose line above the annotation.
  - `source` is always the annotation's file and line.
- An annotation in a nested instruction file (`pkg/AGENTS.md`) gets `scope = "pkg"`: its diff
  checks only see files under `pkg/`.

## Globs

Path parameters (`paths`, `except`, `if_changed`, ...) take gitignore-style globs:

- Paths are repo-relative and use `/`, on every OS: `src/app.py`.
- A pattern without `/` matches the file name at any depth: `*.bak` matches `a/b/x.bak`.
- A pattern with `/` is anchored at the repo root: `src/*.py` matches `src/a.py`, not
  `src/sub/a.py` or `lib/src/a.py`.
- `**` matches zero or more whole directories: `src/**/*.py` matches `src/a.py` and
  `src/x/y/a.py`.
- `*` and `?` never cross `/`.
- `[abc]` and `[!abc]` are character classes.
- A trailing `/` matches everything below that directory: `vendor/` is `vendor/**`.
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
code does not fail the rule.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `pattern` | regex | required | Text that must not be added. |
| `paths` | globs | all files | Files to look in. |
| `except` | globs | `[]` | Files to skip. |
| `ignore_case` | bool | `false` | Case-insensitive match. |

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

Fails when a file in scope lacks a matching added line.

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
| `when_paths` | globs | always | Apply the rule only when the agent edited a matching file; otherwise it passes. |
| `edit_paths` | globs | all | Edits that count for `after_last_edit`. |
| `ignore_edit_paths` | globs | `[]` | Edits that do not count (docs, notes). |

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
last counted edit (the agent's report), finds claims, and ignores negated sentences ("I couldn't
run the tests"). For each claim it looks for an evidence command after the last counted edit.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `claims` | list | all | Which claims to check (table below). |
| `ignore_edit_paths` | globs | `[]` | Edits that do not count as "last edit". |

| Claim | Example phrases | Evidence |
| --- | --- | --- |
| `tests` | "all tests pass", "42 passed" | pytest, unittest, jest, vitest, go test, cargo test, `npm test`, ... |
| `lint` | "lint is clean", "ruff passes" | ruff, flake8, eslint, golangci-lint, clippy, ... |
| `types` | "type-checks cleanly", "mypy passes" | mypy, pyright, tsc, ... |
| `build` | "the build succeeds", "it compiles" | build commands |
| `format` | "formatted with black" | formatters |
| `commit` | "I committed" | `git commit` |
| `push` | "pushed to origin" | `git push` |

Outcomes per claim: no evidence command fails ("claimed but not done"); an evidence command with
a non-zero exit fails ("claimed but failing"); an evidence command with an unknown exit is
`unverified`.

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
