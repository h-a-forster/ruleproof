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
decision. The exception is a transcript too long to read in time: then rules that need it are
not waved through ([Long sessions](#long-sessions)).

## Setup

1. Install ruleproof where the hook's shell can find it:

   ```sh
   uv tool install git+https://github.com/h-a-forster/ruleproof@v0.1.1
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
   `MultiEdit` and `NotebookEdit` become a file edit (`file_path` / `notebook_path`), anything
   else a tool call. An edit is an add when the file does not exist yet, or when it exists but
   not at `--base` (a file Claude created earlier in the session); otherwise it is a modify.
   So a `forbid-change` rule with `actions = ["modify", "delete"]` on `tests/**` ("never edit
   existing tests") lets Claude keep editing the new tests it wrote;
3. runs the `error`-severity rules whose check is `forbid-command`, `forbid-edit`, `forbid-tool`
   or `forbid-change` against that event (no git diff, so it is fast). A `forbid-command` or
   `forbid-tool` rule with `unless_user` or `unless_after` also reads the transcript
   (`transcript_path`), since its verdict depends on earlier user messages or commands; only the
   proposed call can fail it. The transcript is read incrementally and within a time budget;
   see [Long sessions](#long-sessions);
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
| `--base REF` | `HEAD` | Files absent at this ref count as new. Checked with one `git cat-file` call, only for an edit of an existing file when a `forbid-change` rule is active; if git cannot answer, the on-disk check stands. |
| `--transcript-budget SECONDS` | `5` | Time the transcript read may take; see [Long sessions](#long-sessions). `0`: no limit. |

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
| `--transcript-budget SECONDS` | `40` | Time the transcript read may take; see [Long sessions](#long-sessions). `0`: no limit. |

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

## Long sessions

Claude Code lets a call through when its hook times out. A hook that re-read a long transcript
on every call would therefore stop enforcing its rules, without a word, in exactly the long
sessions where rules are most often broken. Two things prevent that.

**Incremental reads.** The hooks keep, per transcript, the parse state up to the last complete
line in the user cache directory (`%LOCALAPPDATA%\ruleproof\transcripts` on Windows,
`~/Library/Caches/ruleproof/transcripts` on macOS, `$XDG_CACHE_HOME/ruleproof/transcripts` or
`~/.cache/ruleproof/transcripts` elsewhere; with `RULEPROOF_CACHE_DIR` set, its `transcripts`
subfolder). A call parses only what was appended since the last one. The result is the same as
a full read: a half-written last line is read again next time, and a transcript that shrank, or
whose first 4 kB or 4 kB before the cached offset changed (a rewrite), is read again from the
start, as is one whose cache is missing, corrupt, from another ruleproof version or cannot be
written. Concurrent calls each replace the cache in one step, so none sees a partial file. The
PreToolUse hook keeps a cache of its own without tool output, which its rules do not read (except
for a call that failed without an exit code: it may have been denied, and then does not count).
The caches hold what the transcript holds (messages, commands, truncated output), never go into
the repo, and entries untouched for 30 days are removed (only files ruleproof wrote are ever
deleted). `RULEPROOF_HOOK_CACHE=0` turns them off: every call reads the whole transcript.

**A time budget that fails closed.** If the transcript still cannot be read in time (the first
call in a session that is already long, or after a ruleproof upgrade), the read stops at the
budget, saves its progress so the next call continues from there, and the hook says so on
stderr. It does not wave the rules through:

- PreToolUse denies a call that only a `unless_user` / `unless_after` rule could allow, and
  tells Claude the transcript was not read in time and to make the call again. Calls that need
  no history are judged as usual.
- Stop blocks once, naming the error rules it could not check and telling Claude not to report
  the work as checked against them. Loop protection still applies, so Claude is not trapped.

The defaults (5 s PreToolUse, 40 s Stop) are about half and two thirds of the timeouts in the
setup above, leaving room for starting Python, the diff and the rules. If you raise a hook's
`timeout`, raise `--transcript-budget` with it; `--transcript-budget 0` removes the limit and
restores the old behaviour of reading to the end however long it takes. Failing closed is the
default because the alternative is the silent failure this section is about; a denied call or an
extra Stop turn is visible and recovers on the next call.

**Measured** with `scripts/bench_hook_transcript.py --mb 1000` on a synthetic 1 GB transcript
with large tool results: one `ruleproof hook` process per call, rules `forbid-command` with
`unless_user`, `require-command` and `claims`, default budgets. Call 1 starts with no cache;
calls 2 to 10 each follow an append of a few records, as in a live session. "Before" is
201f26b, which read the whole transcript on every call; "after" is the current code. Two runs
of each, back to back; wall time per call on a Windows desktop:

| Hook | Before: calls 1 to 10 | After: call 1 (no cache) | After: calls 2 to 10 |
| --- | --- | --- | --- |
| PreToolUse | 2.2 to 2.4 s, median 2.3 s (one call 10.0 s) | 2.5 and 2.6 s | 0.38 to 0.49 s, median 0.42 s |
| Stop | 2.5 to 3.0 s, median 2.6 to 2.8 s (one call 5.6 s) | 3.2 and 3.6 s | 1.0 to 1.3 s, median 1.1 s |

The caches for the 1 GB transcript were 6.9 MB (PreToolUse) and 51 MB (Stop); writing them is
most of what a first call costs beyond a full read. Times rise several-fold when the disk is
busy: an earlier run on the same machine under heavy load measured 16 s per PreToolUse call
before, and a first call of 10.7 s with an earlier version of the incremental reader.

Limits:

- The budget bounds loading the cache and reading the transcript, not starting Python, saving
  the cache or running the rules. Under heavy load a first call can still run past
  PreToolUse's 10 s timeout (10.7 s in the loaded run above), and Claude Code then lets that
  call through. On sessions of a GB or more, set the PreToolUse `timeout` to 30 and keep the
  budget at 5.
- Each call still loads its whole cache and, when lines were appended, rewrites it, so the
  per-call cost grows with the session: about 7 MB of cache per GB of transcript for
  PreToolUse, 50 MB for Stop.
- The first calls in a session that is already long (or after a ruleproof upgrade, which
  starts a new cache) refuse history-dependent calls until the read catches up, about one
  budget's worth of transcript per call.

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
