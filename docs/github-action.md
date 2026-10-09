# GitHub Action

The action installs ruleproof, runs `ruleproof check` on the pull request's diff (and optionally
an agent transcript), runs `ruleproof doctor`, and fails the job when rules fail. Results go to
the job log (text) and the job summary (Markdown). It runs on Linux, macOS and Windows runners.

## Pull request check

```yaml
on: pull_request

permissions:
  contents: read

jobs:
  ruleproof:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: ruleproof/ruleproof@v0.1.0
```

Use `fetch-depth: 0`. The action compares the base commit with the checked-out tree, so the base
must be in the clone. On a shallow clone it fetches the base itself, falling back to
`git fetch --unshallow`, which is slower than a full checkout.

Transcript rules are skipped unless you pass `transcript`: a CI run has no local agent sessions.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `base` | see below | Git ref to diff against. |
| `rules` | discovery | Rules file. Default: `ruleproof.toml`, then `[tool.ruleproof]` in `pyproject.toml`, plus inline annotations. |
| `transcript` | none | Path to an agent transcript. Empty: `--no-transcript`. |
| `agent` | `auto` | Transcript format: `auto`, `claude-code`, `codex`, `gemini-cli`, `generic`. |
| `fail-on` | `error` | Lowest severity that fails the job: `error`, `warning`, `info`, `never`. |
| `strict` | `false` | Count `unverified` results as failures. |
| `doctor` | `true` | Also run `ruleproof doctor`. Its findings use the same `fail-on`. |
| `sarif` | none | Also write a SARIF report to this path. |
| `version` | action ref | ruleproof version from PyPI, e.g. `0.1.0`. Empty: install from the action's own checkout, so the code matches the ref in `uses:`. |
| `python-version` | `3.13` | Python used to run ruleproof (3.11+). |
| `working-directory` | `.` | Repository to check. `rules`, `transcript` and `sarif` paths are relative to it. |

Default `base`, by event:

| Event | Base |
| --- | --- |
| `pull_request`, `pull_request_target` | `github.event.pull_request.base.sha` |
| `merge_group` | `github.event.merge_group.base_sha` |
| `push` | `github.event.before` (the previous tip of the branch) |
| anything else, or a new branch | `HEAD~1` |

## Outputs

| Output | Meaning |
| --- | --- |
| `failed` | Number of results that fail the run (check + doctor), at or above `fail-on`; `unverified` included with `strict`. With `fail-on: never`, every failing result is counted. |
| `report` | Path to the JSON report of `ruleproof check`. |
| `doctor-report` | Path to the JSON report of `ruleproof doctor`, when it ran. |

## Code scanning (SARIF)

```yaml
    permissions:
      contents: read
      security-events: write
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
          persist-credentials: false
      - id: ruleproof
        uses: ruleproof/ruleproof@v0.1.0
        with:
          sarif: ${{ runner.temp }}/ruleproof.sarif
      - if: always() && steps.ruleproof.outputs.report != ''
        uses: github/codeql-action/upload-sarif@v4
        with:
          sarif_file: ${{ runner.temp }}/ruleproof.sarif
          category: ruleproof
```

Pull requests from forks get a read-only token and cannot upload SARIF. The full example in
[`examples/workflows/ruleproof.yml`](../examples/workflows/ruleproof.yml) skips the upload for
them.

## Checking an agent that ran in CI

Pass the agent's transcript to check what it ran and said, not only what it changed.

[`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action) has an
`execution_file` output: a JSON file holding an array of Claude Code SDK messages. Convert it
to stream-json (one message per line) before passing it to ruleproof:

```yaml
      - name: Record the starting commit
        id: start
        run: echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"

      - id: claude
        uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          prompt: ${{ inputs.task }}

      - if: always() && steps.claude.outputs.execution_file != ''
        env:
          EXECUTION_FILE: ${{ steps.claude.outputs.execution_file }}
        run: jq -c '.[]' "$EXECUTION_FILE" > "$RUNNER_TEMP/claude-transcript.jsonl"

      - if: always() && steps.claude.outputs.execution_file != ''
        uses: ruleproof/ruleproof@v0.1.0
        with:
          base: ${{ steps.start.outputs.sha }}
          transcript: ${{ runner.temp }}/claude-transcript.jsonl
          agent: claude-code
```

`base` is the commit before Claude ran, so the diff holds only Claude's changes. See
[`examples/workflows/claude-code-action.yml`](../examples/workflows/claude-code-action.yml).

For Claude Code run directly, use `claude -p --output-format stream-json --verbose > transcript.jsonl`.
For Codex, use `codex exec --json > transcript.jsonl`.

## Security

- Inputs reach the scripts through environment variables, never by expression expansion inside
  `run:`, so a branch name or input value cannot inject shell code.
- ruleproof is installed into its own virtual environment under `$RUNNER_TEMP`. The action does
  not change `PATH` or the Python used by later steps.
- The action needs only `contents: read` (plus `security-events: write` to upload SARIF).
- Nothing is sent over the network except the package install. Transcripts can contain secrets:
  do not upload them as artifacts unless you need to.

## Pinning

`uses: ruleproof/ruleproof@v0.1.0` pins a release. For a stronger pin, use the release's commit
SHA and let Dependabot (`package-ecosystem: github-actions`) update it.
