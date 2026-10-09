# Security

## What ruleproof touches

ruleproof reads local files: your repository, git, and agent session transcripts under
`~/.claude`, `~/.codex` and `~/.gemini` (or files you pass with `--transcript`). Transcripts can
contain secrets: command output, environment variables, file contents, API tokens pasted into a
chat.

- ruleproof never sends data anywhere. It makes no network calls and has no telemetry.
- It never runs commands from a transcript or a rules file. The only program it runs is `git`.
- `ruleproof doctor` reports inline secrets in MCP configs by file, server and variable name,
  never by value.
- Reports quote short excerpts of the evidence (a command line, a diff line, a sentence). Treat
  reports, SARIF files and `ruleproof timeline` exports like the transcripts they come from
  before you upload or share them.

## Supported versions

Security fixes go into the latest release.

## Reporting a vulnerability

Report it privately through GitHub:
[Security > Report a vulnerability](https://github.com/ruleproof/ruleproof/security/advisories/new).
Do not open a public issue. Include the version, what you did, and what happened. Use synthetic
data; do not send real transcripts.

You should get a reply within 7 days. We will agree on a disclosure date with you and credit you
in the advisory unless you prefer otherwise.
