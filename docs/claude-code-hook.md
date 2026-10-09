# Claude Code Stop hook

`ruleproof hook claude-stop` checks Claude Code's work when it tries to finish. If a rule fails,
Claude is told what failed and keeps working until it is fixed. It catches problems while the
session is still open, before a reviewer or CI does.

## What it does

Claude Code runs Stop hooks when the main agent finishes a response. The hook:

1. reads the hook payload from stdin: `transcript_path`, `cwd` and `stop_hook_active`;
2. runs `ruleproof check` in `cwd` against the uncommitted diff (`--base HEAD`) and the session
   transcript at `transcript_path`;
3. if any `error`-severity rule fails, prints
   `{"decision": "block", "reason": "..."}` with the failures; Claude Code then continues the
   session with that reason as feedback;
4. otherwise prints nothing and Claude stops normally.

It never blocks on `warning` or `info` rules, or on `unverified` or `skip` results.

## Setup

1. Install ruleproof where the hook's shell can find it:

   ```sh
   uv tool install ruleproof    # or: pipx install ruleproof
   ```

2. Add rules: a `ruleproof.toml` or inline annotations in `AGENTS.md` / `CLAUDE.md`
   (see [rules.md](rules.md)). Check them by hand first:

   ```sh
   ruleproof check --session latest
   ```

3. Register the hook in `.claude/settings.json` (shared with the team) or
   `.claude/settings.local.json` (only you):

   ```json
   {
     "hooks": {
       "Stop": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "ruleproof hook claude-stop",
               "timeout": 60
             }
           ]
         }
       ]
     }
   }
   ```

   Stop hooks take no `matcher`. If ruleproof is not on `PATH`, use
   `uvx ruleproof hook claude-stop` (slower on first run).

4. Run `/hooks` in Claude Code to confirm the hook is registered.

A complete example is in [`examples/python-service/`](../examples/python-service/).

## Loop protection

A rule Claude cannot satisfy must not trap it in a loop. The hook blocks at most once in a row:
when Claude Code sets `stop_hook_active` (it is already continuing because of a Stop hook), the
hook lets it stop. Claude gets one chance to fix the failures; anything still failing is left
for you, CI, or the next `ruleproof check`.

## Limits

- The diff is uncommitted changes only. If Claude committed during the session, diff rules do
  not see those commits; transcript rules still see the edits and commands.
- Exit codes come from the transcript. Claude Code records failed shell commands as errors, so
  `require-command` and `claims` can tell a failing test run from a passing one.
- The hook reads the local transcript file and runs git. It makes no network calls.

## Disable it

- For good: remove the `Stop` entry from the settings file. Claude Code cannot disable a single
  hook while keeping it configured.
- For yourself only: register the hook in `.claude/settings.local.json` instead of the shared
  `.claude/settings.json`.
- All hooks, temporarily: set `"disableAllHooks": true` in your settings, or start one run with
  `claude --settings '{"disableAllHooks": true}'`.
- One rule: lower its `severity` to `warning`. It still shows in `ruleproof check` but no longer
  blocks.
