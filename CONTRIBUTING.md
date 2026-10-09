# Contributing

Bug reports, new checks and new transcript parsers are welcome. For anything larger than a fix,
open an issue first so we can agree on the shape.

## Setup

You need [uv](https://docs.astral.sh/uv/) and git.

```sh
git clone https://github.com/h-a-forster/ruleproof
cd ruleproof
uv sync                 # creates .venv with Python 3.11+ and the dev tools
uv run ruleproof --help
```

## Checks before you push

```sh
uv run pytest
uv run ruff check
uv run ruff format --check     # `uv run ruff format` to fix
uv run mypy
```

CI runs the same commands on Linux, macOS and Windows with Python 3.11, 3.12 and 3.13, builds the
wheel, and runs the GitHub Action on the pull request. Optionally, install the pre-commit hooks
defined in this repo's `.pre-commit-hooks.yaml` in a test project to try them.

## Ground rules

- No runtime dependencies. The standard library only; dev tools go in the `dev` group.
- Type hints everywhere; `mypy --strict` must pass.
- Open files with `encoding="utf-8"` (`errors="replace"` for transcripts). Paths and line endings
  must work on Windows, macOS and Linux.
- Input from outside (transcripts, diffs, config) must never crash ruleproof. Skip what you
  cannot read and record a warning; raise `ruleproof.errors` types for user errors.
- Tests are fast, use `tmp_path`, and need no network and no home directory.
- **Fixtures must be synthetic.** Never commit a real transcript, even trimmed: they hold paths,
  names, code and secrets. Write the few lines a test needs by hand.

## Adding a check

1. Pick the module: `checks/diff_checks.py` (needs the diff), `checks/transcript_checks.py`
   (needs the transcript) or a new module for a larger check.
2. Write a function `(rule: Rule, ctx: Context) -> RuleResult` and register it:

   ```python
   @register(
       "forbid-symlink",
       needs={"diff"},
       params={"paths": Param("glob_list", default=[], doc="files to check")},
       doc="Fails when the diff adds a symlink.",
   )
   def forbid_symlink(rule: Rule, ctx: Context) -> RuleResult: ...
   ```

   The loader validates and fills `rule.params` from the schema, so the function can index it
   directly. Respect `rule.scope` for diff checks. Return evidence with a path and line, or a
   transcript event index, for every failure.
3. A new module must be imported in `checks.load_all()`.
4. Add tests in `tests/` for pass, fail, skip and (if it applies) unverified.
5. Document it in `docs/rules.md` with a parameter table and a real example, and add it to
   `CHANGELOG.md`.

## Adding a transcript parser

1. Add a module in `src/ruleproof/transcripts/` that turns one file into a `Session`. Map the
   agent's tools to the five event kinds (`user`, `assistant`, `command`, `edit`, `tool`).
   Unwrap shell wrappers (`bash -lc "..."`) and record exit codes when the format has them.
2. Never raise on a bad line: skip it and append to `Session.warnings`.
3. Teach `--agent auto` to recognise the format and, if the agent stores sessions on disk,
   teach `discover.py` where to find them for a repo.
4. Test with small synthetic fixtures under `tests/fixtures/<agent>/` that cover each event
   kind, a failed command, and a malformed line.
5. Document the format in `docs/architecture.md` and `CHANGELOG.md`.

## Releasing

Maintainers: update `version` in `pyproject.toml` and the `CHANGELOG.md` section, merge, then
push a tag `vX.Y.Z`. The release workflow checks the tag matches the version, publishes to PyPI
with trusted publishing, and creates the GitHub release from the changelog section.
