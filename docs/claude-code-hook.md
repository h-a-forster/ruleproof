# Claude Code hooks

ruleproof has two Claude Code hooks. Use either or both:

- `ruleproof hook claude-pretool` (PreToolUse) refuses a tool call **before it runs** when it
  would break a `forbid-*` rule: a `git commit` you forbade, an edit in generated code, a write
  outside the repo. Some actions cannot be undone afterwards, so this is the hook that stops them.
- `ruleproof hook claude-stop` (Stop) checks Claude's work when it tries to finish. If an error
  rule fails, Claude is told what failed and gets one more turn to fix it. This covers the rules
  that need the whole session or the diff: tests run after the last edit, claims backed by
  commands, required changes.

Both always exit 0, never call the network, and stay out of the way when they cannot run: no
rules, an unreadable payload or an internal error gives a one-line diagnostic on stderr and no
decision.

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

3. Register the hooks in `.claude/settings.json` (shared with the team) or
   `.claude/settings.local.json` (only you):

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit",
           "hooks": [
             {
               "type": "command",
               "command": "ruleproof hook claude-pretool",
               "timeout": 10
             }
           ]
         }
       ],
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

   The PreToolUse `matcher` is matched against the tool name. A value made only of letters,
   digits, `_`, `-`, `,` and `|` is a list of exact names; `"*"` (or an empty or missing matcher)
   matches every tool. The list above covers `forbid-command`, `forbid-edit` and `forbid-change`.
   If you have `forbid-tool` rules for other tools (`WebFetch`, MCP tools), use `"*"`. Stop
   hooks take no `matcher`.

   If ruleproof is not on `PATH`, use `uvx ruleproof hook ...` (slower on first run).

4. Run `/hooks` in Claude Code to confirm the hooks are registered.

A complete example is in [`examples/python-service/`](../examples/python-service/).

## PreToolUse: `claude-pretool`

Claude Code runs PreToolUse hooks after Claude proposes a tool call and before it runs. The hook:

1. reads the payload from stdin: `tool_name`, `tool_input` and `cwd`;
2. turns the proposed call into a one-event session: `Bash` / `PowerShell` become a command
   (`tool_input.command`, unwrapped from `bash -lc '...'` and similar), `Write`, `Edit`,
   `MultiEdit` and `NotebookEdit` become a file edit (`file_path` / `notebook_path`; an add when
   the file does not exist yet, else a modify), anything else a tool call;
3. runs the `error`-severity rules whose check is `forbid-command`, `forbid-edit`, `forbid-tool`
   or `forbid-change` against that event (no git diff, so it is fast);
4. if one fails, prints

   ```json
   {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                           "permissionDecisionReason": "ruleproof: this tool call would break ..."}}
   ```

   Claude Code then skips the call and shows Claude the reason: the rule id, its description and
   source line, and the evidence. Write the rule's `description` so it says what to do instead
   ("Never commit; leave committing to the user.");
5. otherwise prints nothing, and the call goes through Claude Code's normal permission flow. The
   hook never approves a call.

The repo is the nearest directory above `cwd` that holds `.git`, or `cwd` itself.

Limits: a shell command is matched as written, so `forbid-command` sees `git commit` but not a
script that runs it; `forbid-edit` and `forbid-change` see Claude's file tools, not files a shell
command writes. The Stop hook and `ruleproof check` still see those through the diff.

| Option | Default | Meaning |
| --- | --- | --- |
| `--rules FILE` | discovery | Rules file to use. A relative path resolves against the payload's `cwd`. The file may live outside the repo. |

## Stop: `claude-stop`

Claude Code runs Stop hooks when the main agent finishes a response. The hook:

1. reads the hook payload from stdin: `transcript_path`, `cwd`, `stop_hook_active` and
   `last_assistant_message`;
2. runs `ruleproof check` in `cwd` against the uncommitted diff (`--base HEAD`) and the session
   transcript at `transcript_path` (adding Claude's final message when the transcript file does
   not have it yet);
3. if any `error`-severity rule fails, prints
   `{"decision": "block", "reason": "..."}` with the failures; Claude Code then continues the
   session with that reason as feedback;
4. otherwise prints nothing and Claude stops normally.

It never blocks on `warning` or `info` rules, or on `unverified` or `skip` results. In a
directory that is not a git repository it skips the diff rules and still runs the transcript
rules.

| Option | Default | Meaning |
| --- | --- | --- |
| `--rules FILE` | discovery | Rules file to use. A relative path resolves against the payload's `cwd`. The file may live outside the repo. |
| `--base REF` | `HEAD` | Git ref the working tree is compared with. |

```json
"command": "ruleproof hook claude-stop --rules ~/.config/ruleproof/personal.toml --base origin/main"
```

### Loop protection

A rule Claude cannot satisfy must not trap it in a loop. The Stop hook blocks at most once in a
row: when Claude Code sets `stop_hook_active` (it is already continuing because of a Stop hook),
the hook lets it stop. Claude gets one chance to fix the failures; anything still failing is
left for you, CI, or the next `ruleproof check`.

### Limits

- By default the diff is uncommitted changes only. If Claude committed during the session, diff
  rules do not see those commits unless you set `--base` (e.g. `--base origin/main`); transcript
  rules still see the edits and commands. To stop the commit itself, use a `forbid-command` rule
  with the PreToolUse hook.
- Exit codes come from the transcript. Claude Code records failed shell commands as errors, so
  `require-command` and `claims` can tell a failing test run from a passing one.
- The hook reads the local transcript file and runs git. It makes no network calls.

## Disable them

- For one shell or session: set `RULEPROOF_HOOK_DISABLE=1` in the environment Claude Code
  starts from. Both hooks then exit at once without checking.
- For good: remove the `PreToolUse` / `Stop` entry from the settings file. Claude Code cannot
  disable a single hook while keeping it configured.
- For yourself only: register the hooks in `.claude/settings.local.json` instead of the shared
  `.claude/settings.json`.
- All hooks, temporarily: set `"disableAllHooks": true` in your settings, or start one run with
  `claude --settings '{"disableAllHooks": true}'`.
- One rule: lower its `severity` to `warning`. It still shows in `ruleproof check` but neither
  hook acts on it.
