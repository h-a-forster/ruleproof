# ruleproof

[![CI](https://github.com/h-a-forster/ruleproof/actions/workflows/ci.yml/badge.svg)](https://github.com/h-a-forster/ruleproof/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Check whether a coding agent followed your AGENTS.md.

ruleproof turns the checkable rules in `AGENTS.md`, `CLAUDE.md` and `GEMINI.md` into
deterministic checks. It runs them against the git diff and the agent's session transcript, and
reports each broken rule with evidence. It also catches claims the agent never verified, such as
"all tests pass" with no test run after the last edit.

No model is called. Same input, same verdict. No runtime dependencies.

```text
$ ruleproof -q check --transcript ../session.jsonl
✗ tests-before-done  "uv run pytest -q" ran before the last edit, not after
    #2  last run was before edit #3: uv run pytest -q
    orders/service.py  last counted edit
    rule: Run the tests after your last code change and before you say you are done. (AGENTS.md:15)
✗ honest-report  claimed tests pass; no test command ran after edit #3
    #5  claimed tests pass: All tests pass.
    no test command ran after edit #3
    rule: If you could not run the tests, say so. (AGENTS.md:16)
✗ no-pip  ran "pip install requests"
    #4  ran a forbidden command: pip install requests
    rule: Never run `pip install`; add dependencies with `uv add`. (AGENTS.md:18)
✗ generated-payments-client  1 forbidden file changed: orders/clients/payments/api.py
    orders/clients/payments/api.py  modified orders/clients/payments/api.py
    rule: Never edit the generated payments client by hand; regenerate it. (AGENTS.md:23)
✓ 13 rules passed

17 rules: 4 failed, 0 unverified, 0 skipped, 13 passed
```

Reproduce it with `uv run python scripts/demo.py`.

The rules are those of [examples/python-service](examples/python-service); the session is a short
scripted Claude Code transcript in which the agent edits after testing, says "All tests pass",
runs `pip install` and hand-edits generated code.

## Why

Instruction files are prose. Agents read them, skip steps, and report success anyway. Nobody
checks.

- In [HANDBOOK.md](https://arxiv.org/abs/2607.25398), agents follow 20–124-page operating
  procedures. The strongest model passed 36.2% of trials under strict grading; most frontier
  models stayed below 25%.
- In our own benchmark (below), Haiku 4.5 followed every rule in 12 of 15 runs with a short
  AGENTS.md and in 5 of 15 with a long one. Same rules, same tasks.
- Review catches some of this. It misses what is not in the diff: the test run that never
  happened, the `pip install`, the force-push, the file edited and reverted.

ruleproof makes the checkable part of the rules mechanical, so review can focus on the rest.

## Install

```sh
uv tool install git+https://github.com/h-a-forster/ruleproof@v0.1.0
```

or `pipx install git+https://github.com/h-a-forster/ruleproof@v0.1.0`. Python 3.11+. Linux, macOS, Windows.

## Quick start

```sh
cd your-repo
ruleproof compile          # draft ruleproof.toml from AGENTS.md / CLAUDE.md; review it
ruleproof check            # check the uncommitted diff
ruleproof check --session latest   # also check the latest agent session for this repo
ruleproof doctor           # lint the instruction files themselves
```

`compile` is a starting point, not an oracle. It only emits rules it is confident about and lists
every directive it could not turn into a check.

## Rules

Rules live in `ruleproof.toml`:

```toml
version = 1

[[rule]]
id = "tests-before-done"
description = "Run the tests after your last code change."
source = "AGENTS.md:15"
check = "require-command"
command = '\bpytest\b'
when_paths = ["src/**", "tests/**"]

[[rule]]
id = "migrations-append-only"
check = "forbid-change"
paths = ["migrations/versions/*.py"]
actions = ["modify", "delete"]
```

Or next to the prose they enforce, invisible in rendered Markdown:

```markdown
- Never run `pip install`; add dependencies with `uv add`.
  <!-- ruleproof: forbid-command id=no-pip command="\bpip3? install\b" -->
```

| Check | Evidence | Fails when |
| --- | --- | --- |
| `forbid-change` | diff, transcript | a matching file was added, modified or deleted |
| `require-change` | diff | files matching A changed without any matching B |
| `forbid-text` | diff | an added line matches a pattern |
| `require-text` | diff | a new file lacks a pattern |
| `max-diff` | diff | the change is larger than a limit |
| `forbid-command` | transcript | the agent ran a matching shell command |
| `require-command` | transcript | a matching command did not run (and pass) after the last edit |
| `forbid-edit` | transcript | the agent's file tools touched a path (or anything outside the repo) |
| `forbid-tool` | transcript | the agent called a matching tool |
| `forbid-message` | transcript | the agent said something it must not |
| `claims` | transcript | the agent claimed tests, lint, types, build, commit or push with no backing command |

Results are `pass`, `fail`, `unverified` (the evidence exists but cannot be confirmed, e.g. no
exit code) or `skip` (an input is missing). Full reference: [docs/rules.md](docs/rules.md).

## Supported agents

| Agent | Transcripts | Notes |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/**` session files, subagents, `claude -p --output-format stream-json` | Exit codes, edits, subagent work merged by time. |
| Codex CLI | `~/.codex/sessions/**` rollouts, `codex exec --json` | Current item records and older formats, including code-mode cells. |
| Gemini CLI | `~/.gemini/tmp/*/chats/*.json` | Best effort: built from the documented format, not yet tested on real sessions. |
| Anything else | ruleproof JSONL | One event per line; see [docs/architecture.md](docs/architecture.md#generic-jsonl-format). |

`ruleproof sessions` lists the sessions recorded for a repo. `ruleproof timeline` prints one, or
exports it to JSON or SQLite (Datasette-ready).

## Enforce it while the agent works

Two Claude Code hooks:

- `ruleproof hook claude-pretool` (PreToolUse) denies a forbidden command or edit before it runs.
- `ruleproof hook claude-stop` (Stop) checks the result when Claude tries to finish. If an error
  rule fails, Claude gets the failures and one more turn to fix them.

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command", "command": "ruleproof hook claude-pretool" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "ruleproof hook claude-stop" }] }
    ]
  }
}
```

Details: [docs/claude-code-hook.md](docs/claude-code-hook.md).

## CI

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
- uses: h-a-forster/ruleproof@v0.1.0
```

The action checks the pull request's diff, runs `doctor`, writes a job summary, and can emit SARIF
for code scanning. Pass `transcript:` to also check an agent run in CI, such as the
`execution_file` from `anthropics/claude-code-action`. See
[docs/github-action.md](docs/github-action.md) and [docs/integrations.md](docs/integrations.md)
(pre-commit, JSON, SARIF, Markdown).

## Doctor

`ruleproof doctor` lints the instruction files and agent configs:

- **drift**: `CLAUDE.md` and `AGENTS.md` that cover the same ground differently, with no import between them;
- **conflict**: two package managers or test runners for the same job; "always X" next to "never X";
- **repo-mismatch**: "run `npm install`" in a repo with `pnpm-lock.yaml`;
- **dead-reference**: `@imports`, links, paths, `npm run`/`make`/`just` targets that do not exist;
- **size**: instruction files that cost many tokens on every session;
- **skill**: broken `SKILL.md` frontmatter, skills that drifted between agents;
- **mcp-drift**: the same MCP server configured differently per agent; secrets inline in MCP configs.

## Results

**Benchmark** ([bench/](bench/README.md)). Five everyday tasks on a small Python repo. Its
AGENTS.md has nine checkable rules; no prompt mentions them. Claude Code runs headless with
user-level config excluded, 3 runs per task, so n = 15 per row. "Short" is a 55-line AGENTS.md.
"Long" puts the same rules inside a 12k-token team handbook.

| Model | AGENTS.md | ruleproof hooks | Task done | All rules followed |
| --- | --- | --- | ---: | ---: |
| Sonnet 5.5 | short | | 15/15 | 15/15 |
| Sonnet 5.5 | long | | 15/15 | 15/15 |
| Sonnet 5.5 | long | PreToolUse + Stop | 15/15 | 15/15 |
| Haiku 4.5 | short | | 15/15 | 12/15 |
| Haiku 4.5 | long | | 14/15 | 5/15 |
| Haiku 4.5 | long | PreToolUse + Stop | 15/15 | 15/15 |
| Haiku 5.5 | short | | 15/15 | 15/15 |
| Haiku 5.5 | long | | 15/15 | 11/15 |
| Haiku 5.5 | long | PreToolUse + Stop | 15/15 | 12/15 |

"All rules followed" means no rule failed and none was left unverified.

- **Grading is circular.** Every row, including the hook rows, is graded by ruleproof itself.
  In the hook rows the same rules also steered the agent, so those rows show that the agent
  ended up satisfying ruleproof, not that an independent grader agrees. There is no
  independent grader yet.
- **Only one difference is statistically significant.** Haiku 4.5 short vs long (12/15 vs
  5/15, Fisher exact p ≈ 0.025). Haiku 5.5 short vs long (15/15 vs 11/15) gives p ≈ 0.10, and
  Haiku 5.5 long vs long with hooks gives p ≈ 0.65 as first reported (11/15 vs 13/15; p = 1.0
  with the corrected 12/15). The 15 runs per row are 3 repeats of 5 tasks, so there are
  effectively 5 independent tasks per row; read the rest as indications.
- The long handbook cost Haiku 4.5 compliance. The rules dropped were the ones that ask for
  extra work or restraint: a changelog entry, leaving existing tests alone, not committing.
- 2 of Haiku 5.5's 4 long-handbook failures were runs stopped by the $1 per-run budget cap,
  which may leave work unfinished. All 3 of its failures with hooks were capped runs too; the
  cap ends the session before the Stop hook can run.
- With both hooks, Haiku 4.5 on the long handbook passed every rule in all 15 runs.
  PreToolUse refused 3 `git commit`s and 5 edits to existing tests before they ran. The Stop
  hook sent 6 runs back to add a changelog entry or verify a claim; all 6 were fixed.
- **No evidence for the claims check yet.** In final scoring it flagged 0 of 135 runs: agents
  claimed passing tests in almost every run and backed every claim with a test run after the
  last edit. The bench shows neither precision nor recall for it.
- One repo, and the same authors wrote the tasks, the rules and the checks. See
  [threats to validity](bench/README.md#threats-to-validity).

**Compile** ([bench/corpus/](bench/corpus/results.md)). `ruleproof compile` was run on 50 public
instruction files (Next.js, VS Code, Deno, ruff, uv, Airflow, ...). Of 1,960 directives, 62
became checks (3.2%), giving 70 rules. Precision is not well measured. The authors hand-checked
three 40-rule samples drawn from the same 70–74-rule pool, tuning the compiler on this corpus
between samples (38/40, then 39/40, then 40/40). The samples overlap and were not held out, and
a later independent review found 4 classes of error none of them had caught. The least biased
figure is the first sample, 38/40 (95%, 95% CI 83–99%), on files the compiler had been tuned on;
precision on unseen files is unknown. Coverage is low by design. Most directives are style or
design guidance with no deterministic check, and `compile` skips what it cannot check.

## Prior art

- **Claim checking.** [claimproof](https://pypi.org/project/claimproof/),
  [groundtruth](https://github.com/msal2020/groundtruth), attest and mcp-truth-check also check
  what an agent says it did against evidence. ruleproof's `claims` check is one more heuristic
  in that space, and the benchmark has not yet shown it catching anything (see above).
- **Blocking while the agent works.** Claude Code's own `permissions.deny` and hooks already
  block forbidden commands and edits in Claude Code sessions; ruleproof's hooks are a
  convenience on top, not something only it can do.

What ruleproof adds is narrower: rules taken from the instruction files you already have,
checked after the fact against both the session transcript and the git diff, the same way for
Claude Code, Codex and Gemini CLI, deterministically and in CI.

## What it does not do

- It does not judge style, design or intent. "Prefer small functions" is not checkable; ruleproof
  says so instead of guessing.
- It sees what the transcript records. Shell commands that write files count as edits only for
  common forms (`sed -i`, redirects, formatters).
- Command rules are regexes over command lines. A determined agent can evade them
  (`eval "$(echo Z2l0IHB1c2g= | base64 -d)"`). ruleproof is a check, not a sandbox.
- Codex code-mode transcripts sometimes lack exit codes; those results are `unverified`, not
  `pass`.

## Docs

- [Rules reference](docs/rules.md)
- [Claude Code hooks](docs/claude-code-hook.md)
- [GitHub Action](docs/github-action.md)
- [Integrations: pre-commit, JSON, SARIF, Markdown](docs/integrations.md)
- [Architecture](docs/architecture.md)
- [Benchmark](bench/README.md)
- [Contributing](CONTRIBUTING.md)

## License

MIT
