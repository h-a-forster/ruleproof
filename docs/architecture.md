# Architecture

ruleproof turns the checkable parts of an agent's instruction files (AGENTS.md, CLAUDE.md,
GEMINI.md, ...) into deterministic checks, then runs them against two kinds of evidence:

- **the diff**: what changed in the repo (git, or a patch file);
- **the transcript**: what the agent did and said (commands, edits, messages).

No model is called at check time. Results are reproducible and CI-safe.

```
AGENTS.md ──compile──> ruleproof.toml ─┐
AGENTS.md (inline <!-- ruleproof: -->) ┼─> rules ─┐
                                        │          ├─> engine ─> results ─> text | json | markdown | sarif
git diff / patch ───────────> Diff ─────┼──────────┤
~/.claude, ~/.codex, ~/.gemini ─> Session ─────────┘
instruction + config files ──────> doctor ────────────> results ─> (same reporters)
```

## Modules

| Module | Role |
| --- | --- |
| `models.py` | Plain dataclasses: `Event`, `Session`, `Diff`, `FileChange`, `Rule`, `RuleResult`, `Evidence`, `Report`, `Context`. |
| `paths.py` | gitignore-style globs, repo-relative path mapping (Windows, MSYS and posix). |
| `transcripts/` | One parser per agent → `Session`. `discover.py` finds sessions on disk. `export.py` writes timelines (text, JSON, SQLite). |
| `diff.py` | `Diff` from git (`base..worktree`, plus untracked files) or from a unified diff. |
| `rules.py` | Load rules from `ruleproof.toml`, `[tool.ruleproof]` in `pyproject.toml`, and inline annotations in instruction files; validate params against the check registry. |
| `checks/` | The registry (`__init__.py`) and the checks: `diff_checks.py`, `transcript_checks.py`, `claims.py`. |
| `engine.py` | Runs each rule; skips rules whose inputs are missing; isolates crashes. |
| `compile.py` | Heuristic prose → rules compiler with a coverage report. |
| `doctor/` | Instruction/config linter: drift, contradictions, dead references, size, skills, MCP configs. |
| `report/` | Reporters: `text.py`, `json.py`, `markdown.py`, `sarif.py`. |
| `hook.py` | Claude Code hook adapters: `claude-stop` (Stop: block the agent from finishing while rules fail) and `claude-pretool` (PreToolUse: deny a tool call that would break a forbid-* rule before it runs). |
| `cli.py` | argparse CLI. |

Runtime dependencies: none (Python ≥ 3.11 standard library only).

## Transcript model

Every parser emits a `Session` whose `events` are ordered and indexed from 0. Five kinds:

| Kind | Meaning | Key fields |
| --- | --- | --- |
| `user` | user message | `text` |
| `assistant` | assistant prose | `text` |
| `command` | shell command | `text` (the command line, unwrapped from `bash -lc` etc.), `exit_code` (None if unknown), `output`, `is_error` |
| `edit` | file written / edited / deleted by a tool | `path` (as recorded), `action` |
| `tool` | any other tool call | `tool`, `text` (short input summary), `output` |

Subagent transcripts (Claude Code `<session>/subagents/**.jsonl`) are merged by timestamp
with `actor` set to the subagent id. Parsers never raise on malformed lines: they skip the
line and append a message to `Session.warnings`.

Supported formats:

- **Claude Code**: `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` and headless
  `claude -p --output-format stream-json` output. Shell tools: `Bash`, `PowerShell`.
  Edit tools: `Write`, `Edit`, `MultiEdit`, `NotebookEdit`. Failed shell commands come back
  as `is_error: true` with content starting `Exit code N`.
- **Codex CLI**: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (and `codex exec --json`
  output). Handles `function_call` `shell` / `exec_command`, `local_shell_call`,
  `custom_tool_call` `apply_patch`, code-mode `exec` cells that call
  `tools.exec_command({cmd: ...})` / `tools.apply_patch(...)`, and `exec_command_end` events.
- **Gemini CLI**: `~/.gemini/tmp/<project-hash>/chats/session-*.json`. Tools
  `run_shell_command`, `write_file`, `replace`.
- **Generic**: ruleproof's own JSONL, one event per line, same fields as `Event`. Any agent can
  be adapted by writing this format; see below.

### Generic JSONL format

One JSON object per line, UTF-8. `--agent auto` recognises the format when a line among the
first few is a session header or has a known `kind`. The first line may be a session header;
every field in it is optional:

```json
{"session": {"agent": "my-agent", "id": "run-42", "cwd": "/home/dev/repo", "started_at": "2026-04-05T06:07:08Z", "model": "local-model"}}
```

`agent` defaults to `generic`, `id` to the file name without its extension. `cwd` is the
directory relative edit paths are resolved against (default: the repo root). A header on any
line but the first is ignored with a warning.

Every other line is one event, in the order it happened:

```json
{"kind": "user", "text": "Add a timeout to order placement.", "timestamp": "2026-04-05T06:07:09Z"}
{"kind": "command", "text": "uv run pytest -q", "exit_code": 0, "output": "12 passed in 0.40s", "tool": "shell"}
{"kind": "edit", "path": "src/orders/service.py", "action": "modify", "tool": "write_file"}
{"kind": "tool", "tool": "web_fetch", "text": "https://example.com/docs", "is_error": false}
{"kind": "assistant", "text": "Done. All tests pass."}
```

| Field | Type | Kinds | Meaning |
| --- | --- | --- | --- |
| `kind` | string, required | all | `user`, `assistant`, `command`, `edit` or `tool` |
| `text` | string | all | message text; the command line for `command`; a short input summary for `tool`; for `edit`, used as `path` when `path` is missing |
| `timestamp` | string | all | ISO-8601 time, shown in timelines |
| `actor` | string | all | `main` (default) or a subagent id; `claims` and `forbid-message` read only `main` by default |
| `tool` | string | all | the agent's own tool name (`forbid-tool` matches it) |
| `path` | string | `edit` | the file, absolute or relative to the session `cwd` |
| `action` | string | `edit` | `add`, `modify` or `delete`; missing or anything else becomes `unknown` (with a warning for an unrecognised value) |
| `exit_code` | integer | `command` | exit status; leave it out when unknown (results then come from the output, or are `unverified`) |
| `output` | string | `command`, `tool` | the output; truncated to 4000 characters (head and tail kept) |
| `is_error` | boolean | `command`, `edit`, `tool` | the agent runtime reported the call as failed; a failed edit changed nothing |

Fields are read regardless of `kind`, but the checks only use them as listed. `index` is
ignored: events are numbered in file order. A line that is not a JSON object, has an unknown
`kind`, or has a field of the wrong type is skipped (or the field is dropped) with a warning in
`Session.warnings`. A file with no header and no valid event is an error.

## Diff model

`diff.from_git(repo, base="HEAD")` compares `base` with the working tree (staged and unstaged)
and adds untracked, non-ignored files as `added`. `diff.from_patch(text)` parses a unified diff.
Paths are repo-relative posix. Binary files have `binary=True` and no `added` lines.

## Rules

A rule is `{id, check, params, description, severity, source, scope}`.

### `ruleproof.toml`

```toml
version = 1

[[rule]]
id = "no-backup-files"
description = "Don't create .bak / .orig copies; use git."
source = "AGENTS.md:14"
check = "forbid-change"
actions = ["add"]
paths = ["*.bak", "*.orig", "*.before_*"]

[[rule]]
id = "tests-before-done"
check = "require-command"
command = '\bpytest\b'
must_succeed = true
after_last_edit = true
when_paths = ["src/**"]
```

Every key that is not a rule field is a check parameter. Unknown checks, unknown or
mistyped parameters, values outside a parameter's `choices`, empty lists for `nonempty`
parameters, unmet `one_of` requirements, and invalid regexes or globs are load errors that
cite `file:line`. Regexes are Python `re`, matched with `search`. The top-level `exclude`
lists globs skipped when scanning for instruction files. The rules file is the first of
`ruleproof.toml`, `.ruleproof.toml` and `pyproject.toml` (same schema under
`[tool.ruleproof]`); files are never merged.

### Inline annotations

A rule can sit next to the prose it enforces, invisible in rendered Markdown:

```markdown
- Never edit generated code in `api/gen/`.
  <!-- ruleproof: forbid-change paths=api/gen/ -->
```

Syntax: `<!-- ruleproof: <check> key=value key="value with spaces" -->`. List values are
comma-separated. `id`, `severity` and `description` are optional; `id` defaults to the
lowercased `<file-stem>-l<line>` (prefixed with the directory for nested files:
`pkg/agents-l7`), `description` to the preceding paragraph or list item, `source` to the
annotation's file and line. Annotations in a nested instruction file (`pkg/AGENTS.md`) get
`scope = "pkg"`. Annotations inside code blocks and inline code are ignored.

## Checks

| Check | Needs | Parameters | Fails when |
| --- | --- | --- | --- |
| `forbid-change` | diff or transcript | `paths` (glob list, required), `except` (glob list), `actions` (`add`, `modify`, `delete`; default all), `include_ignored` (default false: agent edits to gitignored files do not count) | a changed or agent-edited file matches |
| `require-change` | diff | `if_changed` (globs, required), `then_changed` (globs, required), `except` | some file matches `if_changed` but none matches `then_changed` |
| `forbid-text` | diff | `pattern` (regex, required), `paths` (default all), `except`, `ignore_case`, `redact` (hide matches in evidence) | an added line matches |
| `require-text` | diff | `pattern` (regex, required), `paths`, `new_files_only` (default true) | a new file in scope lacks a match |
| `max-diff` | diff | `max_files`, `max_lines` (added + removed), `except` | the diff is larger |
| `forbid-command` | transcript | `command` (regex, required), `ignore_case`, `match_quoted` (default false: quoted strings and heredocs are ignored) | a command matches |
| `require-command` | transcript | `command` (regex, required), `must_succeed` (default true), `after_last_edit` (default true), `when_paths` (globs), `edit_paths` / `ignore_edit_paths` (which edits count), `match_quoted` (as in `forbid-command`) | no matching command ran (after the last counted edit; with exit 0). Exit unknown → `unverified` |
| `forbid-edit` | transcript | `paths` (globs), `outside_repo` (bool) | the agent edited a matching path (even if later reverted), or any path outside the repo |
| `forbid-tool` | transcript | `tool` (regex, required) | the agent called a matching tool |
| `forbid-message` | transcript | `pattern` (regex, required), `ignore_case`, `subagents` (default false: main agent only) | an assistant message matches |
| `claims` | transcript | `claims` (list; default all), `ignore_edit_paths` | the agent claims a result (tests pass, lint clean, types check, build works, formatted, committed, pushed) with no matching successful command after its last edit |

`skip` means an input was missing. `unverified` means the evidence exists but cannot be
confirmed (e.g. the command ran but the transcript has no exit code). `--strict` fails on
`unverified`.

### Claims

`claims` looks at assistant messages after the last counted edit (the agent's report) and
matches each claim's phrases, ignoring negated sentences ("I couldn't run the tests"). For
each claim it looks for its evidence command after the last counted edit:

| Claim | Example phrases | Evidence |
| --- | --- | --- |
| `tests` | "all tests pass", "42 passed", "test suite is green" | pytest, unittest, jest, vitest, go test, cargo test, `npm test`, ... |
| `lint` | "lint is clean", "ruff passes", "no lint errors" | ruff, flake8, eslint, golangci-lint, clippy, ... |
| `types` | "type-checks cleanly", "mypy passes", "no type errors" | mypy, pyright, tsc, ... |
| `build` | "the build succeeds", "it compiles" | build commands |
| `format` | "formatted with black" | formatters |
| `commit` | "I committed", "created a commit" | `git commit` |
| `push` | "pushed to origin" | `git push` |

Outcomes: no evidence → `fail` ("claimed but not done"); evidence with a non-zero exit → `fail`
("claimed but failing"); evidence with unknown exit → `unverified`.

## Doctor

`ruleproof doctor` reads instruction and config files and reports:

| Id | Finds |
| --- | --- |
| `doctor/drift` | two agents' instruction files that cover the same ground with different content and no import between them (`CLAUDE.md` vs `AGENTS.md` without `@AGENTS.md`) |
| `doctor/conflict` | contradictions: different package managers / test runners / formatters named for the same job; `always X` vs `never X` |
| `doctor/repo-mismatch` | instructions that disagree with the repo: `npm install` but `pnpm-lock.yaml`; `pip install` but `uv.lock` |
| `doctor/dead-reference` | referenced files, `@imports`, `npm run x` / `make x` / `just x` targets that do not exist |
| `doctor/size` | instruction files whose estimated token cost per session is high |
| `doctor/skill` | skills with missing / invalid frontmatter, oversized descriptions, duplicates that drifted |
| `doctor/mcp-drift` | the same MCP server configured differently across agents; inline secrets in MCP env |

Files scanned: `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `GEMINI.md` (any depth, ignoring
VCS and dependency directories), `.github/copilot-instructions.md`, `.cursor/rules/*.mdc`,
`.cursorrules`, `.windsurfrules`, `.clinerules`, skills under `.claude/skills`,
`.agents/skills`, `.codex/skills`, MCP configs in `.mcp.json`, `.cursor/mcp.json`,
`.vscode/mcp.json`, `.gemini/settings.json`, `.codex/config.toml`.

## CLI

```
ruleproof check    [--rules FILE] [--repo DIR] [--base REF | --patch FILE | --no-diff]
                   [--transcript FILE | --session latest|ID | --no-transcript]
                   [--agent auto|claude-code|codex|gemini-cli|generic]
                   [--format text|json|markdown|sarif] [--output FILE]
                   [--fail-on error|warning|info|never] [--strict]
ruleproof compile  [FILES...] [--output FILE] [--force]
ruleproof doctor   [--repo DIR] [--format text|json|markdown|sarif] [--output FILE]
                   [--fail-on error|warning|info|never] [--max-tokens N]
ruleproof sessions [--repo DIR] [--agent claude-code|codex|gemini-cli|generic] [--all]
                   [--limit N] [--json]
ruleproof timeline SESSION [--format text|json|sqlite] [--output FILE]
ruleproof checks   # list available checks and parameters
ruleproof hook claude-stop    [--rules FILE] [--base REF]
ruleproof hook claude-pretool [--rules FILE] [--base REF]
```

Every command also takes `-q` / `--quiet` and `--no-color`; `ruleproof --version` prints the
version.

Exit codes: `0` nothing failed at or above `--fail-on`; `1` failures; `2` usage or
configuration error.

## Claude Code hooks

Both hooks read the hook payload as JSON on stdin and always exit 0. `--rules FILE` (relative
to the payload's `cwd`) picks the rules file. Any internal problem (no rules, a broken
transcript, a bug) lets the call through with a one-line diagnostic on stderr, and
`RULEPROOF_HOOK_DISABLE=1` turns both off.

`ruleproof hook claude-stop` (Stop) reads `transcript_path`, `cwd`, `stop_hook_active` and
`last_assistant_message`, runs `check` against the diff from `--base` (default `HEAD`, so the
uncommitted changes) and the transcript, and when rules fail prints
`{"decision": "block", "reason": "..."}` so the agent fixes them before finishing. It never
blocks twice in a row (`stop_hook_active`), and never blocks on `unverified` or `warning`.

`ruleproof hook claude-pretool` (PreToolUse) turns the proposed call (`tool_name`,
`tool_input`) into a one-event session and runs the error-severity `forbid-command`,
`forbid-edit`, `forbid-tool` and `forbid-change` rules on it. When one fails it prints
`{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
"permissionDecisionReason": "..."}}`, so the call does not run and Claude sees the rule. Shell
tools (`Bash`, `PowerShell`) become commands, file tools (`Write`, `Edit`, `MultiEdit`,
`NotebookEdit`) edits, and anything else a tool call. An edit of a file that does not exist
yet is an add; with `--base REF` (default `HEAD`), editing a file that exists now but not at
`REF` also counts as an add, so a rule against modifying existing files does not stop the
agent from editing a file it created in this session.
