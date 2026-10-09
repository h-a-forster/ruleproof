# Integrations

How to use ruleproof's output outside the terminal. For CI, see
[github-action.md](github-action.md); for Claude Code, see
[claude-code-hook.md](claude-code-hook.md).

## pre-commit

Add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/h-a-forster/ruleproof
    rev: v0.1.0
    hooks:
      - id: ruleproof-diff
      - id: ruleproof-doctor
```

| Hook | Runs | Use |
| --- | --- | --- |
| `ruleproof-diff` | `ruleproof check --no-transcript --base HEAD` | Diff rules against what is about to be committed. |
| `ruleproof-doctor` | `ruleproof doctor` | Lint instruction files and agent configs. |

Both run on every commit (`always_run`) and ignore the file list. pre-commit stashes unstaged
changes first, so `ruleproof-diff` sees the staged changes. Untracked files are not stashed and
count as added files; add them to `.gitignore` or stage them.

Pass extra options with `args`, e.g. `args: [--fail-on, warning]`.

## JSON report

`--format json` writes a stable report, schema `ruleproof/report@1`. `check` and `doctor` use
the same shape. New keys may be added within `@1`; existing keys keep their meaning.

Top level:

| Key | Type | Meaning |
| --- | --- | --- |
| `schema` | string | `"ruleproof/report@1"` |
| `kind` | string | `"check"` or `"doctor"` |
| `version` | string | ruleproof version |
| `repo` | string or null | Repository root |
| `base` | string or null | Git ref the diff was taken against |
| `session` | object or null | `{agent, id, path}` of the transcript used |
| `summary` | object | Counts: `total`, `failed`, `unverified`, `skipped`, `passed` |
| `results` | list | One object per rule (below) |
| `notes` | list of strings | Run notes, e.g. why rules were skipped |

Each result:

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | string | Rule id |
| `check` | string | Check name, e.g. `forbid-command` |
| `status` | string | `pass`, `fail`, `unverified` or `skip` |
| `severity` | string | `error`, `warning` or `info` |
| `summary` | string | One-line outcome |
| `description` | string | The instruction in words |
| `source` | string or null | Where the prose rule lives, e.g. `AGENTS.md:14` |
| `origin` | string | Where the rule was defined, e.g. `ruleproof.toml:31` |
| `evidence` | list | `{message, path, line, event, excerpt}`; `path`/`line` locate a file, `event` is a transcript event index; unused fields are null |

Example: list failing rule ids with `jq`:

```sh
ruleproof check --format json --fail-on never | jq -r '.results[] | select(.status == "fail") | .id'
```

## SARIF

`--format sarif` writes SARIF 2.1.0 for GitHub code scanning and other SARIF viewers. Each
failing or unverified rule is one result. It points at the first file location in its evidence,
or, for transcript evidence, at the instruction line the rule came from. Severities map to
`error`, `warning` and `note`.

```sh
ruleproof check --format sarif --output ruleproof.sarif
```

Upload with `github/codeql-action/upload-sarif` (see [github-action.md](github-action.md)).

## Markdown for PR comments

`--format markdown` writes GitHub-flavoured Markdown: a verdict line, a table of failing and
unverified rules with evidence, and collapsed sections for skipped and passed rules. Text from
rules, diffs and transcripts is escaped, so it cannot inject markup. The GitHub Action writes it
to the job summary. To post it as a pull request comment:

```yaml
    permissions:
      contents: read
      pull-requests: write
    steps:
      # ... checkout with fetch-depth: 0, install ruleproof ...
      - run: ruleproof check --base "$BASE" --no-transcript --format markdown --fail-on never --output "$RUNNER_TEMP/ruleproof.md"
        env:
          BASE: ${{ github.event.pull_request.base.sha }}
      - run: gh pr comment "$PR" --body-file "$RUNNER_TEMP/ruleproof.md"
        env:
          GH_TOKEN: ${{ github.token }}
          PR: ${{ github.event.pull_request.number }}
```

Pull requests from forks get a read-only token and cannot comment.
