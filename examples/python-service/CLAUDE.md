@AGENTS.md

## Claude Code

- Hooks in `.claude/settings.json` run ruleproof. `ruleproof hook claude-pretool` refuses a
  command or edit that breaks a rule before it runs; follow the rule it quotes instead.
  `ruleproof hook claude-stop` checks your work when you finish; if it blocks you, fix what it
  lists, then finish again.
