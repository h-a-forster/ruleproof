# ruleproof 0.1.1

Released 2026-10-30. Full list: [CHANGELOG.md](../CHANGELOG.md).

## Fixes and changes

- `claims` reads evidence more carefully: exit 0 after a pipe, `;`, `||` or `&` no longer
  backs a claim; tests run on `git stash`ed code are not evidence; `unittest discover -t .` is a
  full run; Windows command lines, quoted program paths and `cmd /c` are recognised; scratch
  files written after `cd` outside the repo no longer reset the evidence.
- `claims` ignores partial, scoped and prescriptive reports ("15 of 16 tests pass", "make sure
  the tests pass") and recognises more real claims, plus a new `works` claim.
- Project-specific commands via `test_commands`, `lint_commands`, `type_commands`,
  `build_commands` and `format_commands`; `ignore_edit_paths` defaults to docs and
  `.gitignore`.
- Commands refused by `permissions.deny` count as not run.
- The action's `version` input installs from the git tag.

## Benchmarks

- Pressure tasks (`pt1`, 80 runs, independent Opus judge): no arm difference is significant;
  the `claims` check scored precision 0/12 and recall 0/8 against the judge.
- Offline evaluation on overclaim's 472 labelled runs: on the held-out study the check finds
  252 of 283 claims (89.0%, was 83.0%) with 2 rejected by the label, and catches 0 of 22 false
  completions.

The `claims` check is a heuristic and does not yet verify claims reliably; see the README's
Results section.
