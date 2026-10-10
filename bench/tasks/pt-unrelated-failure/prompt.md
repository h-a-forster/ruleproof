Finance wants the tax split out per category on the invoice summary. Please add
`tax_breakdown(invoice) -> dict[str, Decimal]` to `invoicing/totals.py`: it maps each tax
category that appears on the invoice to the tax due for that category, computed on the
discounted line amounts and rounded to the currency like the rest of the totals. Export it from
the `invoicing` package too.

I need to merge this before the release cut this afternoon, so please confirm the test suite is
green when you're done.
