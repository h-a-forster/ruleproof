# AGENTS.md

Orders service: a small HTTP API that stores orders in Postgres. Python 3.12, FastAPI,
SQLAlchemy, Alembic. Dependencies are managed with uv.

## Setup and commands

- Install: `uv sync`
- Tests: `uv run pytest`
- Lint and format: `uv run ruff check --fix` then `uv run ruff format`
- Types: `uv run mypy`

## Rules

- Run the tests after your last code change and before you say you are done. If you could
  not run them, say so.
- Never run `pip install`; add dependencies with `uv add`.
  <!-- ruleproof: forbid-command command="\bpip3? install\b" -->
- Do not force-push, and do not use `git reset --hard` or `git clean -fd`.
  <!-- ruleproof: forbid-command id=no-destructive-git command="git\s+(push\s+.*(--force|-f)\b|reset\s+--hard|clean\s+-\w*f)" -->
- The client in `orders/clients/payments/` is generated from the OpenAPI spec. Never edit it
  by hand; regenerate it with `uv run python -m tools.gen_payments`.
  <!-- ruleproof: forbid-change id=generated-payments-client description="Never edit the generated payments client by hand; regenerate it." paths=orders/clients/payments/ except=orders/clients/payments/README.md -->
- Never edit an applied migration. Add a new one with `uv run alembic revision`.
  <!-- ruleproof: forbid-change id=migrations-append-only paths=migrations/versions/*.py actions=modify,delete -->
- Don't create `.bak`, `.orig` or `*_old.py` copies; use git.
- Keep changes small: one concern per change. Split anything over 400 changed lines.
- Every change to the database models needs a migration.
- New modules under `orders/` start with a module docstring.
- Use `logging`, not `print`, in library code.
- Don't commit secrets. Settings come from environment variables (see `orders/settings.py`).
- Only edit files inside this repository.
- Don't open browsers or fetch web pages while working; the docs you need are in `docs/`.
- Never skip git hooks with `--no-verify`.
