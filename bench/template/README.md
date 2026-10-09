# invoicing

Invoice arithmetic and rendering used by the billing service: line items, discounts, tax by
category, currency formatting, and a plain-text invoice layout for emails.

```
uv sync
uv run pytest
```

## Layout

- `src/invoicing/models.py`: `Invoice` and `LineItem`.
- `src/invoicing/money.py`: rounding and currency formatting.
- `src/invoicing/totals.py`: subtotal, discount, tax and total.
- `src/invoicing/render.py`: the plain-text invoice used in emails.
- `src/invoicing/generated/`: currency and tax-rate tables generated from `spec/invoicing.toml`
  by `scripts/codegen.py`.

## Example

```python
from datetime import date
from decimal import Decimal

from invoicing import Invoice, LineItem, render_invoice

inv = Invoice(
    number="INV-1001",
    customer="Acme GmbH",
    issue_date=date(2024, 3, 1),
    currency="EUR",
    items=[LineItem("Consulting", Decimal("3"), Decimal("120.00"), "standard")],
)
print(render_invoice(inv))
```
