---
description: synthetic instruction file for the compiler tests
---

# Widget service

<!-- Table of contents
- [Setup](#setup)
-->

- [Setup](#setup)
- [Rules](#rules)

## Setup

The service lives in `src/widget/` and talks to the billing API.
Always use `uv`, not pip.

| Command | Purpose |
| --- | --- |
| `make test` | never run in CI |

## Rules

- Run `uv run pytest -q` before committing.
- Make sure `ruff check .` passes before you open a PR.
- Never run `git push --force`.
- Do not use `--no-verify` to skip hooks.
- Never edit `src/widget/generated/` by hand; run `make gen` instead.
- Don't create backup files (`.bak`, `.orig`); use git.
- Update `CHANGELOG.md` for every user-facing change.
- Don't leave `print()` calls or `breakpoint()` in the code.
- Never commit unless the user asks you to.
- Don't touch files outside the repository.
- Never add new dependencies without asking first.
- Prefer small functions.
- Write clear commit messages.

```bash
# this fence is an example, not a rule
git push --force
```

## Never

- `npm install`
- Commit secrets or `.env` files.

Before you finish, run:

```bash
cargo clippy --all-targets
```
