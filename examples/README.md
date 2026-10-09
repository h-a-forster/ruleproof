# Examples

| Path | What it shows |
| --- | --- |
| [`python-service/`](python-service/) | Instruction files and rules for a small Python service. Only the agent-facing files are included, not the service code. |
| [`python-service/AGENTS.md`](python-service/AGENTS.md) | Prose rules, some with inline `<!-- ruleproof: ... -->` annotations. |
| [`python-service/CLAUDE.md`](python-service/CLAUDE.md) | Imports `@AGENTS.md`, so Claude Code and other agents read the same rules. |
| [`python-service/ruleproof.toml`](python-service/ruleproof.toml) | One rule for every check type, each citing its AGENTS.md line. |
| [`python-service/.claude/settings.json`](python-service/.claude/settings.json) | Registers the Claude Code PreToolUse and Stop hooks. |
| [`workflows/ruleproof.yml`](workflows/ruleproof.yml) | Pull request check with the GitHub Action, results uploaded as code scanning alerts. |
| [`workflows/claude-code-action.yml`](workflows/claude-code-action.yml) | Runs Claude Code in CI with `anthropics/claude-code-action`, then checks its commits and transcript. |

Try the rules on your own repo:

```sh
cp examples/python-service/ruleproof.toml .
ruleproof checks                          # what each check does
ruleproof check --no-transcript           # diff rules against uncommitted changes
ruleproof check --session latest          # add the latest local agent session
```

See [docs/rules.md](../docs/rules.md), [docs/github-action.md](../docs/github-action.md),
[docs/claude-code-hook.md](../docs/claude-code-hook.md) and [docs/integrations.md](../docs/integrations.md).
