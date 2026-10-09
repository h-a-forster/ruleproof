# AGENTS.md

This is the engineering handbook for the `invoicing` library. It is written for everyone who
touches this repository: the Billing Core engineers who own it, engineers from other teams who
send us changes, and the coding agents we use for routine work. It is long because it collects
several years of decisions in one place. You do not need to read it top to bottom before every
change, but the sections on money, reference data, logging, testing and review describe how we
actually work, and changes that ignore them tend to bounce in review.

The handbook is maintained by Billing Core. When something here is out of date, the fix is a
normal change to this file, reviewed like any other. When the handbook and the code disagree,
the code is usually right about behaviour and the handbook is usually right about intent; point
out the mismatch in your summary so we can reconcile the two.

## About this repository

`invoicing` is a small Python library that turns a list of line items into invoice totals and
renders the plain-text invoice that Larkspur Systems emails to its customers. It handles line
items with quantities and unit prices, a per-invoice percentage discount, tax by category, the
currency table (codes, names, symbols and minor units), and a fixed-width text layout used in the
body of invoice emails.

It is deliberately narrow. It does not talk to databases, it does not send email, it does not
know about customers beyond the display name printed on the invoice, and it does not know what
day it is. All of that lives in the billing service, which calls into this library at a few
well-defined points. Keeping the library pure (inputs in, values out) is the main reason it has
stayed easy to reason about while the service around it has grown.

The current release is 0.4.0. The public API is what `src/invoicing/__init__.py` exports:
`Invoice`, `LineItem`, `InvoiceTotals`, `compute_totals`, `format_money`, `render_invoice` and
`round_money`. Everything else is internal, even if it does not start with an underscore, but
treat internal helpers that other modules import (`line_total`, `quantum`) with some care too,
because the billing service has been known to reach past the public surface.

### How to use this handbook

The sections roughly follow the life of a change: setting up, understanding the platform and
the library, the rules for money and APIs, logging, tests, style, the changelog, review, and
finally the operational material (security, retention, on-call) that mostly concerns the
service rather than this repository. A glossary and an FAQ close the document.

A lot of the operational material is here for context. Agents working on a small change in
this repository will never deploy anything, page anyone or touch customer data, but knowing why
the service is built the way it is explains many of the constraints on the library.

## Getting set up

We use uv for everything: `uv sync` to set up, `uv run ...` to run things. Never `pip install`
anything (that includes `uv pip install`); the lockfile is the only source of truth for the
environment. Python is 3.11+, so `tomllib`, `dataclasses` and friends are all available.

The repository pins the interpreter version in `.python-version`, and uv picks it up
automatically. The project metadata, the dependency groups and the pytest configuration all
live in `pyproject.toml`; there is no `setup.py`, no `setup.cfg` and no `requirements.txt`, and
we have no plans to add them. The package is built with hatchling, but you will rarely need to
build it locally. The billing service consumes it as a pinned, released version, not from a
checkout of this repository.

After `uv sync` you have a virtual environment in `.venv/` with the package installed in
editable mode, so changes under `src/` are visible immediately without reinstalling. The
virtual environment is ignored by git and is safe to delete; `uv sync` recreates it from the
lockfile in a few seconds.

A few practical notes:

- Everything works offline once the environment exists. Nothing in the library, the tests or
  the code generator needs the network.
- The repository is developed on Linux, macOS and Windows. Use `pathlib` rather than string
  paths in scripts, and open text files with an explicit `encoding="utf-8"`. The currency
  table contains non-ASCII symbols, and the default encoding on Windows is not UTF-8.
- Generated files are written with `\n` line endings regardless of platform. If your editor
  shows a whole-file diff after you open one of them, it is your editor, not the file.
- There is no Makefile and no task runner. The handful of commands you need are listed in this
  handbook and in the README.

## The billing platform at a glance

Larkspur sells subscriptions and professional services to business customers in the EU, the UK
and the US. Billing is handled by a set of services owned by three teams. This section is a
short tour, so that the constraints on this library make sense.

### The billing service

The billing service (`billing-service`, owned by Billing Core) is the system of record for
invoices. It receives usage and subscription events, assembles invoices on the billing run,
stores them in its Postgres database, and hands them to the notification service for delivery.
It is a Python application deployed as a set of containers: an API process, a scheduler that
drives the nightly billing run, and a pool of workers that process invoice jobs.

The service imports this library as a normal dependency, pinned to a released version in the
service's own lockfile. It uses it in three places:

1. When an invoice is assembled, the worker builds `LineItem` and `Invoice` objects from the
   database rows and calls `compute_totals`. The resulting `InvoiceTotals` is stored alongside
   the invoice and is the amount the customer is charged.
2. When an invoice is finalised, the worker calls `render_invoice` and stores the text body.
   That body is what the notification service later puts into the email.
3. The customer portal API calls `format_money` to show amounts in the web UI, so the portal
   and the email always format numbers the same way.

Because totals are persisted and charged, a change to arithmetic in this library is a change to
what customers pay. Because the rendered body is persisted, a change to the layout affects
every invoice finalised after the service picks up the new version, while older invoices keep
their stored text. Both facts shape the review rules described later.

### Ledger sync

`ledger-sync` (owned by Finance Engineering) copies finalised invoices into the accounting
system once an hour. It does not use this library directly; it reads the stored totals. It does,
however, reconcile them against the accounting system's own calculation, and it raises an alert
when the two differ by more than zero. This is why our rounding has to match the accounting
system exactly: half up, per line, before summing. A one-cent drift that nobody notices in a
unit test shows up as hundreds of reconciliation alerts after a billing run.

### Notifications

The notification service (`notify`, owned by the Messaging team) sends all customer email. For
invoices it wraps the stored text body in a template with a greeting, the payment instructions
and the legal footer, and sends it as a `text/plain` part with an HTML alternative generated
from the same text. Messaging owns the templates and the footer; we own the body. The body is
inserted verbatim, which is why the layout has a fixed width and why anything that leaks into
the body ends up in front of customers.

### Dunning

The dunning worker (part of the billing service) sends reminders for unpaid invoices. It reads
`due_date` and `paid` from stored invoices and re-renders the invoice body for the reminder
email. That is the only place where an already-finalised invoice is rendered again, and it is
the reason `Invoice.paid` exists: a paid invoice renders with a thank-you line instead of a
payment prompt.

### Where this library fits

The short version: this library is a pure function from invoice data to numbers and text. The
service owns persistence, scheduling, delivery and everything with side effects. When a feature
request seems to need the library to look something up, read a setting or remember state
between calls, the right answer is almost always to pass the value in as an argument and let
the service decide where it comes from.

## Ownership and contacts

The table below lists who owns what. It is here so that summaries can point at the right people
and so that reviewers know whose approval a change needs. Names are current as of the last
handbook revision.

| Area                                   | Owning team         | Primary contact     |
|----------------------------------------|---------------------|---------------------|
| `invoicing` library (this repository)  | Billing Core        | Mira Okafor         |
| `billing-service`                      | Billing Core        | Mira Okafor         |
| Currency and tax reference data        | Billing Core + Tax  | Jonas Albrecht      |
| `ledger-sync` and reconciliation       | Finance Engineering | Priya Raman         |
| `notify`, email templates and footer   | Messaging           | Daniel Chu          |
| Releases of this library               | Billing Core        | Tomas Lindqvist     |
| Production deploys of billing          | Platform            | Platform on-call    |
| Security review and dependency review  | Security            | Hana Sato           |

Within this repository, module ownership is informal but real:

- `models.py` and `totals.py` are owned by Mira Okafor and Jonas Albrecht. Changes to totals
  need a second reviewer from Billing Core, and changes that alter amounts for existing inputs
  also need a sign-off from Finance Engineering.
- `money.py` is owned by Jonas Albrecht. It is short and heavily depended on; keep it that way.
- `render.py` is owned by Billing Core with Messaging as a required reviewer for any change to
  what customers see.
- `spec/invoicing.toml` is owned jointly by Billing Core and the Tax team. Tax decides rates;
  Billing Core decides how they are represented.
- `scripts/codegen.py` is owned by Billing Core.

Ownership here is about review, not about who is allowed to write code. Anyone can propose a
change to any module; the owners make sure it gets the right eyes.

## Dependencies and supply chain

Please don't add dependencies, runtime or dev. This package ships inside the billing service and
every dependency needs a security review, so solve it with the standard library. If you really
think something is needed, say so in your summary and we will discuss it.

Some background on why this is so strict. The billing service runs with access to customer
payment data and to the accounting system's API. The Security team reviews every package that
ends up in its environment, including transitive dependencies, and re-reviews on major version
upgrades. The review is not a formality: it covers maintainership, release signing, install-time
behaviour and what the code does at import. A review typically takes two to four weeks. Because
this library is installed into the service, anything it depends on goes through the same
process, and so does anything in its dev group, because the service's build image installs the
dev group to run the library's tests during the release check.

The good news is that the standard library covers almost everything this package needs.
`decimal` handles money, `dataclasses` handles the data model, `tomllib` reads the spec,
`datetime` handles dates, `logging` handles diagnostics, `textwrap` and plain string formatting
handle the layout, and `json` is used by the code generator to produce correctly escaped string
literals. When you find yourself reaching for a package, it is worth checking whether a few lines
of standard library code would do. They usually would, and they are easier to review than a new
package.

For reference, the current state of `pyproject.toml` is: no runtime dependencies, `pytest` as the
only member of the dev group, and hatchling as the build backend. `uv.lock` pins the exact
versions. The lockfile changes only when the dependency set changes, so in practice it should not
appear in your diff at all.

Some related notes on supply chain hygiene, mostly for the service but relevant here too:

- Release artefacts of this library are built by the release manager from a tagged commit, on
  the shared build runners, never on a laptop.
- The service installs this library from Larkspur's internal package index, which mirrors only
  reviewed packages. Our releases are uploaded there by the release tooling.
- Vendoring third-party code (copying a module into `src/`) counts as adding a dependency for
  review purposes. Please do not do it to get around the review. Short, well-understood snippets
  from the Python documentation are fine with a comment saying where they came from.

## Library architecture

The package is four modules plus a generated subpackage. The dependency direction is strict and
simple, and it is worth keeping that way:

```
generated  <-  models  <-  money  <-  totals  <-  render
```

More precisely: `generated` depends on nothing in the package; `models` and `money` depend only
on `generated`; `totals` depends on `generated`, `models` and `money`; `render` depends on
`models`, `money` and `totals`. Nothing imports `render`. There are no import cycles, and a
change that introduces one is a sign that something is in the wrong module.

### `models.py`

`models.py` defines the two data classes, `LineItem` and `Invoice`.

`LineItem` is frozen. It has a description, a quantity, a unit price and a tax category, which
defaults to `"standard"`. Validation happens in `__post_init__`: quantities must be positive,
unit prices must not be negative, and the tax category must exist in the generated tax table.
Credits are not modelled as negative line items; the service issues credit notes through a
separate flow that does not use this library yet.

`Invoice` is a regular (mutable) data class because the service builds it incrementally. It has
a number, a customer display name, an issue date, a currency code (default EUR), a list of
items, a discount percentage between 0 and 100, payment terms in days (default 30) and a `paid`
flag. Its `__post_init__` checks that the currency exists in the generated currency table and
that the discount is within range. `due_date` is a property computed from the issue date and the
payment terms; it is never stored on the object.

A note on mutability: the service never mutates an `Invoice` after it calls `compute_totals` or
`render_invoice`, and the library must not mutate the objects it is given. Functions in this
package treat their arguments as read-only.

### `money.py`

`money.py` is about representing amounts, not computing invoices. `quantum(currency)` returns
the smallest unit of a currency as a `Decimal` (for example `Decimal("0.01")` for two decimals).
`round_money(amount, currency)` rounds half up to that unit. `format_money(amount, currency)`
produces the display string used both in emails and in the customer portal, with the currency
symbol, a thousands separator and a leading minus sign for negative amounts.

Formatting is intentionally not locale-aware. The service sends invoices in English with a
fixed number format regardless of the customer's country, because that is what the accounting
team and the legal footer assume. Localised formatting has been discussed several times and is
not on the roadmap for this library.

### `totals.py`

`totals.py` computes `InvoiceTotals` (subtotal, discount, tax and total) from an `Invoice`.
Each line total is the quantity times the unit price, rounded to the currency's minor unit.
The subtotal is the sum of the rounded line totals. The discount and tax are each rounded once,
and the total is derived from the rounded parts, so that the printed numbers always add up. The
docstring of `compute_totals` is the authoritative description of the intended order of
operations; if you change the arithmetic, keep the docstring and the code in agreement.

`line_total` is also used by `render.py`, so that the amount printed on each line is exactly the
amount that went into the subtotal.

### `render.py`

`render.py` produces the plain-text body. It is described in more detail in the section on
rendering below.

### `generated/`

The `generated` subpackage contains `CURRENCIES` (a dict from currency code to a `Currency`
named tuple) and `TAX_RATES` (a dict from category name to a `Decimal` rate). It also defines
the `Currency` type. The next section explains where these come from.

## Reference data and code generation

Currencies and tax rates change rarely, but when they change they change for legal or
commercial reasons, and the change has to be reviewable by people who do not read Python. That
is why they live in a TOML file rather than in code.

The tables in `src/invoicing/generated/` are generated from `spec/invoicing.toml`. Never edit
those files by hand: change the spec and run `uv run python scripts/codegen.py`, so the spec and the
regenerated files always change together.

### The spec format

`spec/invoicing.toml` has two kinds of tables. Each currency is a table under `currencies`,
keyed by its ISO 4217 code, with a `name`, a `symbol` and the number of `decimals` in its minor
unit. Tax rates live in a single `tax_rates` table that maps a category name to a rate written
as a string, for example `standard = "0.20"`. Rates are strings on purpose: TOML floats would
be parsed as binary floating point, and we never want a binary float anywhere near a rate.

A few conventions for the spec:

- Keep currencies in alphabetical order by code, and tax categories in a sensible order with
  `standard` first. The generator sorts its output anyway, but a tidy spec is easier to review.
- Currency names are the English names used by ISO 4217, in sentence case ("Pound sterling",
  "US dollar").
- The symbol is the one we print on invoices. Where a currency has no widely recognised symbol,
  Billing Core and Messaging agree on what to print; in the past we have used the ISO code
  followed by a space.
- Tax categories are lower-case single words. The category names are part of the API, because
  the billing service stores them on line items, so renaming one is a breaking change.

### What the generator does

`scripts/codegen.py` reads the spec with `tomllib`, validates it, and writes three modules:
`currencies.py`, `tax_rates.py` and `__init__.py`. It validates that each currency's `decimals`
is between 0 and 4 and that each tax rate is at least 0 and below 1, and it exits with a message
if the spec violates either. It only rewrites files whose content would change, and it prints the
name of each file it wrote. Every generated file starts with a header comment that says where it
came from.

The generator also has a `--check` mode, which exits with status 1 and lists the stale files if
the generated modules do not match the spec. Reviewers use it to confirm that a diff touching the
spec is consistent, and it is a cheap sanity check to run yourself after any change in this area.

The generated code uses `json.dumps` to produce string literals, which guarantees correct
escaping of any character, including the non-ASCII currency symbols. If you change the
generator, keep the output deterministic: sorted keys, stable formatting, and `\n` line endings.
Deterministic output is what makes the `--check` mode meaningful.

### Changing the generator itself

Changes to `scripts/codegen.py` are rare and get extra review, because a subtle change there can
silently alter every table. If the generator's output format changes, the regenerated files will
change in the same diff, and the reviewer will look at both together. Keep the generated modules
free of logic: they should contain data and the minimal type definitions needed to describe it.
Behaviour belongs in the hand-written modules.

### Who decides reference data

Tax rates are decided by the Tax team, not by engineers. A change to a rate is always driven by a
ticket from Tax that cites the legal basis and the effective date. The library has no notion of
effective dates; the billing service picks up a new library release on the effective date, which
is coordinated by the release manager. Adding a currency is a commercial decision owned by the
Revenue Operations group, and it usually comes with changes in the payment provider configuration
and the portal, which are outside this repository.

## Money and arithmetic

This is the most important section of the handbook. Everything else is about keeping code tidy;
this is about charging customers the right amount.

### Decimal, always

Money is always `decimal.Decimal`, never `float`. This applies to amounts, unit prices,
quantities, discount percentages and tax rates. Binary floating point cannot represent most
decimal fractions exactly, and the errors are small enough to pass casual tests and large enough
to break reconciliation.

Some specific rules that follow from this:

- Construct decimals from strings or integers: `Decimal("0.20")`, `Decimal(3)`. Never construct a
  `Decimal` from a float literal, and never pass a float through `str()` to get one.
- Start sums with `Decimal(0)`, as `compute_totals` does, so that an empty invoice yields a
  `Decimal` rather than the integer `0`.
- Do not mix `Decimal` and `float` in comparisons or arithmetic. Python raises on some mixed
  operations and silently succeeds on others, and the silent cases are the dangerous ones.
- Do not change the global decimal context (`decimal.getcontext()`) in library code. The service
  shares the process with other code that relies on the default context. If you need a specific
  rounding mode, pass it to `quantize` explicitly, as `round_money` does.
- Percentages are expressed on a 0 to 100 scale (`discount_pct=Decimal("12.5")` means 12.5
  percent), while tax rates are fractions (`Decimal("0.20")` means 20 percent). This asymmetry is
  historical, it is visible in the public API, and it is not going to change. Name new variables
  so that the scale is obvious.

### Rounding

Rounding is half up (`ROUND_HALF_UP`), to the currency's minor unit, because that is what our
accounting system does. It is not banker's rounding, which is Python's default for `round()` and
for `Decimal` arithmetic in the default context. Always round through `round_money` rather than
calling `quantize` with your own arguments, so that there is exactly one place that defines how
we round.

Lines are rounded before they are summed. The amount printed on a line is the amount the customer
sees, and the subtotal has to equal the sum of the printed lines, otherwise a customer with a
calculator will (correctly) report that the invoice does not add up. The existing tests pin this
behaviour for amounts that land exactly on a half cent.

Round as late as possible within a single step and exactly once per step. Rounding an
intermediate value twice (for example rounding a line, scaling it and rounding again) can move
an amount by a cent in either direction, and these errors are hard to spot in review.

### Currencies with other minor units

Every currency currently in the table has two decimals, but the code must not assume that. The
generator accepts between 0 and 4 decimals, and `quantum` derives the unit from the table. Code
that hard-codes `Decimal("0.01")`, multiplies by 100 to get cents, or formats with `.2f` is
wrong for currencies such as the Japanese yen (0 decimals) or the Kuwaiti dinar (3 decimals),
even though the current tests would not catch it.

### Negative amounts

Line items cannot be negative, but negative amounts do appear: discounts are displayed as
negative amounts on the invoice, and the portal shows refunds and credits. `format_money` puts
the minus sign before the currency symbol (`-$3.00`, not `$-3.00`), and rounds before deciding
on the sign so that tiny negative values do not print as `-$0.00`.

### Things that are deliberately out of scope

The library does not convert between currencies, does not handle multiple currencies on one
invoice, does not compute withholding tax, and does not implement tax-inclusive pricing. Each of
these has been requested at some point. Each of them is handled, where it is handled at all, in
the billing service or in the accounting system. If a task seems to require one of them, it is
worth reading the task again; usually a narrower change is what is actually wanted.

## API conventions

The billing service, the portal API and at least one internal reporting job import this
library. Its public API changes slowly and deliberately.

### What is public

Public names are the ones listed in `__all__` in `src/invoicing/__init__.py`. When you add a new
public function or class, export it there and keep `__all__` sorted. Data class fields and
properties of public classes are public too: adding a field with a default is compatible, while
removing or renaming one is not.

### Compatibility

We follow semantic versioning in spirit, while the version is below 1.0: minor releases may
contain carefully announced breaking changes, patch releases never do. In practice we try hard
not to break anything at all. In particular:

- Do not remove or rename public names. If something has to go, keep the old name working, emit a
  `DeprecationWarning` through the `warnings` module with `stacklevel=2`, and note it in the
  changelog. The old name is removed in a later minor release, after the service has migrated.
- Do not change the meaning of existing arguments or the type of existing return values.
- Do not change default values of existing parameters. The service relies on several defaults
  (`currency="EUR"`, `payment_terms_days=30`, `tax_category="standard"`) without passing them.
- Adding a new optional parameter at the end of a signature, or a new keyword-only parameter, is
  compatible.

### Shape of new functions

For new public functions we prefer keyword-only arguments after the first one or two obvious
positional arguments, for example `def summarise(invoice: Invoice, *, include_paid: bool = False)`.
This is a preference, not a rule, and existing functions are not going to be changed to match.
Prefer plain functions over classes with one method, and frozen data classes over dicts for
structured return values, as `InvoiceTotals` does.

Functions should take the values they need as arguments rather than reading global state. Today
the only global state in the library is the generated tables, which are effectively constants.
Functions that need "today" take a `date` argument; the library never calls `date.today()`. This
keeps behaviour deterministic and tests simple.

Return new objects instead of mutating arguments. If a caller needs a modified invoice, give
them a function that returns one (`dataclasses.replace` works well for this) and leave the
original alone.

### Naming

Names follow the domain vocabulary in the glossary at the end of this handbook. We say
"line item", not "row" or "entry"; "subtotal" means the sum of rounded lines before discount and
tax; "total" means the amount due. Currency codes are always called `currency` and are always
upper-case ISO 4217 strings. Tax categories are called `tax_category` on line items and
`category` elsewhere.

## Error handling

The library has a simple error model: validate early, raise built-in exceptions with useful
messages, and let the caller decide what to do.

### Where validation happens

Inputs are validated when the data classes are constructed. By the time a function receives an
`Invoice`, it can assume the currency exists, every line item has a known tax category, quantities
are positive, prices are not negative and the discount is in range. Lower-level helpers such as
`round_money` and `format_money` assume they are given a valid currency code and do not repeat
the check; an unknown code there surfaces as a `KeyError` from the table lookup, which is the
expected behaviour for a programming error.

When you add a new field with constraints, validate it in `__post_init__` alongside the existing
checks, in the same style.

### Which exceptions

- `ValueError` for invalid values supplied by the caller, as the existing validation does.
- `TypeError` only where Python itself would raise it; we do not add explicit `isinstance`
  checks for arguments that are already described by type hints.
- `KeyError` from table lookups, as described above.

We do not define custom exception classes. The service maps `ValueError` from this library to a
"rejected invoice" state with the message attached, and that mapping is simple precisely because
there is only one kind of error to handle. If a new failure mode really needs to be
distinguishable, raise the discussion in your summary rather than inventing a hierarchy.

### Messages

Error messages are lower-case, say what was wrong and include the offending value, for example
`unknown tax category 'luxury'` or `discount must be between 0 and 100, got 120`. Use `!r` for
strings so that empty strings and whitespace are visible. Messages end up in the service's
rejected-invoice report, which is read by the billing operations team, so they should make sense
without the code at hand. Do not include customer names or line descriptions in messages beyond
what is needed to identify the problem.

### What not to do

Do not catch exceptions just to log and re-raise them, do not catch broad `Exception` in library
code, and never swallow an exception to return a default amount. A wrong total that looks
plausible is far worse than a crash: a crash stops one invoice and alerts a human, a wrong total
gets charged.

## Logging and observability

The billing service ships logs from all of its processes to Lumen, our central log store, and
keeps them for 30 days. Logs from this library show up there under the `invoicing.*` logger
names, tagged with the invoice number by the service's logging context.

Library code logs through the `logging` module (`logger = logging.getLogger(__name__)`). No
`print()` in `src/`: the service captures stdout and anything printed ends up in customer emails.

### Conventions

- Create one module-level logger per module, exactly as `totals.py` does, and only in modules
  that actually log something.
- Use %-style arguments rather than f-strings in log calls
  (`logger.debug("invoice %s: subtotal=%s", number, subtotal)`), so that the message is only
  formatted when the record is actually emitted. The billing run processes hundreds of thousands
  of invoices and the formatting cost adds up.
- Never configure logging in library code: no `logging.basicConfig`, no handlers, no level
  changes, no changes to the root logger. Configuration belongs to the application. The service
  sets the `invoicing` logger to `INFO` in production and `DEBUG` in staging.
- Do not log and raise for the same problem. The service logs exceptions with their traceback
  when it handles them; logging in the library as well produces duplicate entries.

### Levels

| Level     | Use in this library                                                         |
|-----------|-----------------------------------------------------------------------------|
| `DEBUG`   | Intermediate values useful when investigating a specific invoice.           |
| `INFO`    | Rare. Something a billing operator would want to see on a normal run.       |
| `WARNING` | Unusual but handled input, for example a deprecated code path being used.   |
| `ERROR`   | Not used. Errors are raised as exceptions and logged by the caller.         |

### What may be logged

Invoice numbers, currency codes, tax categories and amounts may be logged at any level. Customer
display names and line item descriptions are customer data: they may appear at `DEBUG` only, and
only when they are genuinely needed to investigate a problem. Never log anything that looks like
contact details, addresses or payment information; the library should not receive such data in
the first place, and if it ever does, that is a bug in the caller worth reporting.

### Metrics and tracing

The library emits no metrics and creates no tracing spans. The service wraps calls into the
library in spans named after the public function (`invoicing.compute_totals` and so on) and
records durations itself. If you think a metric is missing, the right place for it is the
service, which owns the metrics client.

## Invoice rendering and email

`render_invoice` produces the text body that customers read. It is the most visible part of the
library and the part most likely to generate support tickets when it changes.

### Layout

The layout is exactly 60 characters wide (`WIDTH` in `render.py`). The header has the invoice
number and customer on the left and the issue and due dates on the right. Below a rule of `=`
characters comes a column header (description, quantity, unit price, amount), then one line per
item, then a rule of `-` characters and the totals block: subtotal, discount if there is one,
tax, another `=` rule and the total. The body ends with either a payment prompt with the due date
or, for paid invoices, a thank-you line. The text always ends with a single newline.

Descriptions longer than 27 characters are truncated to 24 characters followed by `...`, so that
columns stay aligned. Quantities are printed without trailing zeros (`3`, not `3.000`). Amounts
are right-aligned and formatted with `format_money`.

### Why plain text, and why 60 characters

The notification service generates the HTML version of the email from the text body, and
customers who read mail in plain text (a surprising number of finance departments) see the body
exactly as we produce it. Sixty characters fits comfortably in every mail client we have tested,
including the narrow preview panes of mobile clients, without wrapping. A line that wraps in a
mail client breaks the column alignment for the rest of the invoice.

### Rules for changing the layout

- Keep every line at or below `WIDTH` characters for all supported currencies and realistic
  amounts. Remember that currency symbols differ in length when written as ISO codes, and that
  amounts in the millions need more room than the examples in the README.
- The body is inserted verbatim into the email between Messaging's greeting and footer. Do not
  add greetings, signatures, legal text or links to the body; those belong to Messaging's
  templates.
- Dates are printed in ISO format (`2024-03-01`). Finance teams in our three markets disagree
  about every other format, and ISO is the one they all accept.
- Any change to what customers see needs a Messaging reviewer, as described under ownership.
  Include a before-and-after sample of a rendered invoice in your summary so reviewers can see the
  change without running anything.

### Re-rendering

Dunning re-renders invoices that were finalised with older library versions. A layout change
therefore affects reminders for old invoices as soon as the service picks up the new release.
That is accepted, but it is a reason to keep layout changes small and well announced.

## Testing

The test suite is small, fast and entirely deterministic. It runs in well under a second on a
laptop, it needs no network, no database and no fixtures from outside the repository, and it
should stay that way.

Tests live in `tests/` and run with `uv run pytest`. Run the full suite before you finish and
make sure it passes; don't report a change as done with failing or unrun tests.

### What the suite covers

The existing test modules map onto the library modules: `test_models.py` covers validation and
the due date, `test_money.py` covers rounding and formatting, `test_totals.py` covers the
arithmetic, and `test_render.py` covers the email layout, including exact expected output for a
reference invoice. The totals tests include the cases that have bitten us before: amounts that
land exactly on a half cent, invoices where per-line rounding and whole-invoice rounding would
give different subtotals, and discounts on zero-rated invoices. Several of the expected values
carry a comment with the hand calculation, which is a habit worth keeping.

### Running a subset

While iterating, it is fine to run a single module or a single test, for example
`uv run pytest tests/test_totals.py` or `uv run pytest -k discount`. Pytest is configured in
`pyproject.toml` with `-q` and with `tests` as the test path, so a bare `uv run pytest` picks up
everything. Add `-x` to stop at the first failure, or `-vv` to see full assertion diffs, which is
useful for the rendering tests where a single misplaced space matters.

### Writing tests

Don't modify existing test files. They pin behaviour other teams rely on, and changes to them
get missed in review. Put new tests in a new file (for example `tests/test_<feature>.py`). If an
existing test is genuinely wrong, say so in your summary instead of changing it.

When you write tests, follow the style of the existing ones:

- Plain functions and plain `assert` statements. No test classes, no `unittest`.
- Amounts are written as `Decimal("...")` string literals and compared with `==`. Compare
  decimals exactly; an approximate comparison on money hides precisely the bugs we care about.
- When an expected amount is not obvious, add a short comment with the calculation.
- Use fixed dates (`date(2024, 3, 1)`), never `date.today()`.
- Use obviously fictional customer names. "Acme GmbH" is the house favourite; "X" is acceptable
  when the name does not matter.
- Prefer one behaviour per test with a descriptive name (`test_discount_on_zero_rated_invoice`)
  over long tests that check many things.
- Use `pytest.raises(ValueError, match=...)` for validation errors, matching a distinctive part
  of the message rather than the whole string.

For rendering, an exact expected string is better than a handful of `in` checks, because the
whole point of the layout is that every character is where we expect it. Build the expected text
from a list of lines joined with `"\n"`, which keeps the columns visible in the source.

### What we do not test here

The library's tests do not exercise the billing service, the email templates or the ledger
reconciliation. The service has its own integration tests that run against each new library
release before it is deployed, and Finance Engineering runs a reconciliation replay against a
month of anonymised production invoices before any release that changes arithmetic. Those happen
in the release process and are not something a change in this repository needs to arrange.

## Style guide

Follow the existing code. The library is small enough that its current style is the style
guide; this section writes down the parts that are easy to miss.

- Every module starts with a one-line module docstring, followed by
  `from __future__ import annotations`, then imports.
- Imports are grouped as standard library, then the package itself, separated by a blank line,
  and sorted alphabetically within each group. Import from the package with absolute imports
  (`from invoicing.money import round_money`), not relative ones.
- Type hints on every function signature, including `-> None`. Use built-in generics
  (`list[LineItem]`, `dict[str, Decimal]`) and `X | None` rather than `typing.List` or
  `Optional`.
- Line length is 100 characters. We do not run an automatic formatter in this repository; keep
  the formatting consistent with the surrounding code by hand, and do not reformat lines you are
  not otherwise changing, because unrelated formatting changes make the review diff harder to
  read.
- Double quotes for strings. f-strings for building text, %-style only in logging calls.
- Small functions with descriptive names. A function that needs a comment to explain each of its
  sections probably wants to be several functions.
- Constants in upper case at module level (`WIDTH`, `HUNDRED`). Avoid magic numbers in
  arithmetic; `Decimal(100)` should be `HUNDRED` when it means "percent".
- No wildcard imports, no mutable default arguments (use `field(default_factory=list)` in data
  classes), no module-level side effects beyond defining names and loggers.

Tooling-wise, the only tool we rely on in this repository is pytest. If your editor runs a
linter or type checker locally, that is your business and can be helpful, but its configuration
does not belong in the repository and its suggestions are not a reason to touch code you are not
otherwise changing.

## Documentation and docstrings

The README is the user-facing documentation for the library: a one-paragraph description, the
two commands needed to get started, the module layout and a short example. It is read by
engineers on other teams who are deciding whether the library does what they need. Keep it
short. If a change adds a public function that other teams are likely to use, a line in the
README's layout section or a short addition to the example is welcome.

Docstrings are for behaviour that is not obvious from the name and signature. `round_money` has
one because "half up, as our accounting system does" is not something you would guess, while
`render_invoice` has none because what it does is clear. Write docstrings in the imperative or
descriptive mood, consistent with the module you are in, and use double backticks for code in
docstrings, as the existing ones do. Document units and scales whenever they could be ambiguous:
whether a percentage is 0 to 100 or 0 to 1, whether an amount is rounded, which currency it is
in.

Comments explain why, not what. A comment like `# 360 * 0.20 + 80.50 * 0.07` next to an
expected value in a test is useful; a comment like `# add the tax` above `tax += ...` is not.

This handbook is documentation too. If you find that it is wrong, mention the discrepancy in
your summary.

## Changelog and releases

`CHANGELOG.md` is how the billing service, Finance Engineering and Messaging find out what
changed between versions. They read it when deciding whether a new release needs extra testing on
their side, so it has to be complete and it has to be written for them, not for us.

Every change under `src/` needs an entry in `CHANGELOG.md` under `## Unreleased`, in the usual
Keep a Changelog sections (Added / Changed / Fixed). One line per change is plenty.

### Writing a good entry

Write entries from the point of view of someone who uses the library. "`render_invoice` prints
the due date under the issue date" tells a reader what they will see; "refactor header
rendering" does not. Mention public names in backticks. If a change alters amounts or output for
existing inputs, say so plainly, because that is the single most important thing a downstream
reader needs to know. Look at the existing entries for the tone: short, concrete, one line each.

Use the sections as Keep a Changelog defines them: Added for new features, Changed for changes in
existing behaviour, Fixed for bug fixes. Deprecated and Removed exist in the format too and are
used rarely. Sections appear in the order Added, Changed, Deprecated, Removed, Fixed, and only
the sections that have entries appear at all.

Released sections are history. Leave them as they are, even if an old entry has a typo.

### How releases happen

Releases are cut by the release manager for this library, currently Tomas Lindqvist, with Mira
Okafor as backup. Contributors do not release, and do not touch the version number in
`pyproject.toml`; the release manager handles both as part of cutting a release.

The release process, for context:

1. The release manager decides the version number from the Unreleased entries: a patch release
   if there are only fixes, a minor release otherwise.
2. They move the Unreleased entries into a new section headed with the version and date, update
   the version in `pyproject.toml`, and tag the release commit as `invoicing-vX.Y.Z`.
3. The shared build runners build the wheel from the tag and publish it to the internal package
   index.
4. Billing Core opens a change in `billing-service` that bumps the pinned version. The service's
   integration tests and, for arithmetic changes, Finance Engineering's reconciliation replay run
   against it.
5. Platform deploys the service through the normal staged rollout: staging, then one production
   worker pool, then the rest.

Releases are not cut during the month-end freeze (the last two and first three business days of
each month), when the large billing runs happen, and changes to tax rates are released to match
their legal effective date. The release manager keeps track of both.

## Code review and version control

Every change to this repository is reviewed by at least one Billing Core engineer, and by the
additional reviewers listed under ownership when it touches their areas. Reviews happen on the
diff, which is why the shape of the diff matters as much as the code in it.

Don't commit. Leave your changes in the working tree so we can review the diff. Also don't leave
backup copies around (`*.bak`, `*.orig`, `*_old.py` and the like); git already has the history.

### What reviewers look for

Reviewers in this repository read every changed line and ask a fairly fixed set of questions:

- Does the change do what the task asked, and nothing else? Unrelated clean-ups, drive-by
  renames and formatting changes are the most common reason a change is sent back. If you notice
  something worth fixing outside the task, mention it in your summary instead.
- Could it change an amount for an existing invoice? If so, is that intended, is it in the
  changelog, and is there a test with a hand-calculated expected value?
- Is money handled as described in this handbook: decimals throughout, rounding through
  `round_money`, no hard-coded minor units?
- Does it keep the public API compatible?
- Could anything new reach the rendered email, and has Messaging seen it if so?
- Is the diff free of noise: no stray debug output, no editor files, no temporary scripts, no
  files that the ignore rules would normally exclude?

### Summaries

When you finish a piece of work, write a summary for the reviewer. Say what you changed and why,
which files are affected, how you verified it (which commands you ran and what they reported),
and anything you were unsure about or chose not to do. If you ran into something in the
repository that looks wrong but was outside the task, list it. A good summary is short and
specific; the reviewer will read the diff anyway, so the summary should tell them what the diff
cannot.

### After review

Once a change is approved, the reviewer takes care of getting it into the main branch. Branch
naming, commit history and merge strategy are handled by the people doing that, and are not
something a contribution needs to prepare for.

## Security

The library has a small attack surface because it does very little, and most of the rules below
are about keeping it that way.

- No dynamic code execution: no `eval`, no `exec`, no `pickle`, no `importlib` tricks. The
  generated modules are plain Python produced by our own generator from a reviewed spec, and are
  imported normally.
- No subprocesses, no network access and no file access in library code. The only file the
  project reads is the spec, and only the code generator reads it, at development time.
- No environment variables in library code. Configuration is the service's job.
- Treat all string inputs (customer names, descriptions, invoice numbers) as untrusted text. They
  come from the service's database, which in turn gets some of them from customer input in the
  portal. The renderer only places them in a plain-text body, which is safe, but code that ever
  uses them in another context (file names, HTML, log formats) needs to be careful.
- Secrets never belong in this repository. The library needs none, the tests need none, and the
  spec contains none.

Vulnerabilities in billing systems are reported to the Security team through their private
intake, not through public issues or shared channels. That process is for humans on the team;
if something in this repository looks like a security problem, describe it in your summary and
the reviewer will take it from there.

Security also reviews dependencies, as described earlier, and audits the billing service once a
year. The last audit's findings about this library were limited to documentation, which is a
standard we would like to keep.

## Data retention and customer data

This section mostly concerns the billing service and the people who operate it. It is included
so that the reasons behind some library conventions are clear.

- Finalised invoices, including their totals and rendered bodies, are retained in the billing
  database for ten years to meet tax record-keeping obligations in our markets. They are never
  edited after finalisation; corrections are issued as credit notes.
- Draft invoices that are never finalised are deleted by the service after 90 days.
- Service logs in Lumen are kept for 30 days. This is one reason the library must not log
  customer data above `DEBUG`: production runs at `INFO`, and the retention policy for logs is
  much shorter than the policy for invoices, so customer data in logs would be a compliance
  problem even before it is a privacy problem.
- Customer deletion requests are handled by the service's privacy tooling, which pseudonymises
  customer records while keeping the invoices that tax law requires us to keep.
- Support tickets and incident reports must not contain customer personal data. Refer to invoices
  by invoice number and to customers by their internal account ID.
- Staging and development environments use synthetic data only. Copying production invoices to a
  laptop or into a test fixture is not allowed, even anonymised. Test data in this repository is
  invented by hand.

None of this requires anything special from a change to the library, other than not logging or
embedding customer data and keeping test data fictional.

## On-call and incidents

Billing Core runs a primary and a secondary on-call rotation for the billing service. Platform
runs the rotation for the infrastructure underneath it. This repository has no on-call of its
own: if a library release causes a problem, it shows up as a billing service incident and is
handled there.

### When things go wrong

The nightly billing run starts at 01:00 UTC and normally finishes by 03:30 UTC. The most common
alerts are:

- reconciliation differences raised by `ledger-sync`, which almost always point to a rounding or
  tax-rate problem;
- an elevated rate of rejected invoices, which usually means input validation is rejecting data
  the service considers valid, or vice versa;
- delivery failures in `notify`, which are rarely related to this library.

The on-call engineer's first move for a suspected library regression is to roll the service back
to the previous library version, not to patch the library under pressure. Rolling back is safe
because the library is stateless; invoices finalised with the newer version keep their stored
totals and are corrected afterwards through credit notes if needed.

### Severity and communication

| Severity | Example                                               | Response                      |
|----------|-------------------------------------------------------|-------------------------------|
| SEV1     | Customers charged wrong amounts, billing run halted   | Page primary and secondary    |
| SEV2     | Reconciliation alerts on a subset of invoices         | Page primary                  |
| SEV3     | Cosmetic problem in rendered invoices                 | Ticket, next business day     |

Incident communication is run by the incident commander in the billing incident channel.
Customer-facing communication, including any status page update and any email to affected
customers, is written and sent by Customer Communications, never by engineers directly. After
every SEV1 and SEV2 incident, the on-call engineer writes a blameless review within five
business days. Several rules in this handbook (rounding per line, decimals from strings, no
global decimal context changes) started life as action items from those reviews.

## Glossary

**Billing run.** The nightly job in the billing service that assembles and finalises invoices.

**Credit note.** A document that reduces the amount owed on a finalised invoice. Issued by the
service; not modelled in this library.

**Currency table.** `CURRENCIES` in the generated subpackage: code, English name, display symbol
and number of decimals for each supported currency.

**Discount.** A percentage (0 to 100) applied to the whole invoice, stored as
`Invoice.discount_pct`. Shown on the invoice as a negative amount.

**Due date.** Issue date plus payment terms. Computed, never stored on the `Invoice` object.

**Dunning.** The process of reminding customers about unpaid invoices.

**Finalised invoice.** An invoice whose totals and body have been stored and which can no longer
be edited.

**Line item.** One row on an invoice: description, quantity, unit price and tax category.

**Line total.** Quantity times unit price, rounded half up to the currency's minor unit.

**Minor unit.** The smallest unit of a currency, such as the cent. Determined by the `decimals`
field in the currency table.

**Reconciliation.** `ledger-sync`'s comparison of our stored totals with the accounting system's
calculation.

**Spec.** `spec/invoicing.toml`, the source of the currency and tax tables.

**Subtotal.** The sum of rounded line totals, before discount and tax.

**Tax category.** The name of a tax rate (`standard`, `reduced`, `zero`). Stored on each line
item.

**Total.** The amount due: subtotal minus discount plus tax.

## FAQ

**Why can't I just use `float` and round at the end?**
Because the accounting system rounds each line half up in decimal, and any other approach will
eventually disagree with it by a cent. The section on money explains the details.

**Why half up and not banker's rounding?**
Because that is what the accounting system and our tax advisers use, and what customers expect
when they check an invoice by hand. Banker's rounding is a fine choice for statistics and the
wrong one here.

**Can the invoice show amounts in the customer's local number format?**
Not in this library. The format is fixed and English, on purpose; see the notes on `money.py`.

**Can an invoice have line items in different currencies?**
No. An invoice has exactly one currency. Multi-currency customers receive one invoice per
currency from the service.

**What happens to a zero-amount invoice?**
The library handles it without special cases: all totals are zero in the invoice's currency and
it renders normally. The service decides whether to send it; currently it does not.

**Can I make `Invoice` frozen like `LineItem`?**
Not without coordinating with the service, which builds invoices incrementally. It is on the
long-term wish list.

**Who decides what a new tax category is called?**
The Tax team proposes it and Billing Core agrees the name, because category names are stored on
line items in the service database and are hard to change later.

**Is there a staging version of the library?**
No. The library has releases only. Staging runs the billing service with whatever library
version the service pins, and that is where pre-release verification happens.

**The tests pass but the rendered invoice looks wrong in my mail client. What now?**
Check that your client displays the text part in a monospaced font; many do not by default. If it
still looks wrong, include a sample of the rendered text in your summary and describe what you
saw.

**Where do I ask questions about this handbook?**
Billing Core reads every summary that comes with a change. Questions and suggestions about the
handbook can go there too.
