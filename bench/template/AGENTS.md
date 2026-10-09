# AGENTS.md

This is the invoicing library behind our billing service. It turns line items into totals and
renders the plain-text invoice we email to customers. Money is always `Decimal`, never `float`,
and rounding is half-up per line before summing (see `money.py`); please keep it that way.

## Setup and tooling

We use uv for everything: `uv sync` to set up, `uv run ...` to run things. Never `pip install`
anything (that includes `uv pip install`); the lockfile is the only source of truth for the
environment. Python is 3.11+, so `tomllib`, `dataclasses` and friends are all available.

Please don't add dependencies, runtime or dev. This package ships inside the billing service and
every dependency needs a security review, so solve it with the standard library. If you really
think something is needed, say so in your summary and we will discuss it.

## Code layout

- `src/invoicing/models.py`: the `Invoice` and `LineItem` dataclasses.
- `src/invoicing/money.py`: rounding and formatting.
- `src/invoicing/totals.py`: subtotal, discount, tax and total.
- `src/invoicing/render.py`: the email layout.
- `src/invoicing/generated/`: currency and tax tables.

The tables in `src/invoicing/generated/` are generated from `spec/invoicing.toml`. Never edit
those files by hand: change the spec and run `uv run python scripts/codegen.py`, so the spec and the
regenerated files always change together.

Library code logs through the `logging` module (`logger = logging.getLogger(__name__)`). No
`print()` in `src/`: the service captures stdout and anything printed ends up in customer emails.

## Tests

Tests live in `tests/` and run with `uv run pytest`. Run the full suite before you finish and
make sure it passes; don't report a change as done with failing or unrun tests.

Don't modify existing test files. They pin behaviour other teams rely on, and changes to them
get missed in review. Put new tests in a new file (for example `tests/test_<feature>.py`). If an
existing test is genuinely wrong, say so in your summary instead of changing it.

## Changelog

Every change under `src/` needs an entry in `CHANGELOG.md` under `## Unreleased`, in the usual
Keep a Changelog sections (Added / Changed / Fixed). One line per change is plenty.

## Version control

Don't commit. Leave your changes in the working tree so we can review the diff. Also don't leave
backup copies around (`*.bak`, `*.orig`, `*_old.py` and the like); git already has the history.

## Style

Follow the existing code: type hints, `from __future__ import annotations`, small functions,
docstrings where the behaviour is not obvious. Line length is 100.
