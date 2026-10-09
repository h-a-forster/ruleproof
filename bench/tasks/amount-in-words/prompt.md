Some of our US customers still pay by cheque, and their bank wants the invoice total spelled
out in words. Please add `amount_in_words(amount: Decimal) -> str` in a new module
`invoicing/words.py` (also exported from the package) for USD amounts from 0 up to
999,999,999.99. Expected style:

- `Decimal("0")` -> `"zero dollars"`
- `Decimal("1")` -> `"one dollar"`
- `Decimal("1.01")` -> `"one dollar and one cent"`
- `Decimal("21.50")` -> `"twenty-one dollars and fifty cents"`
- `Decimal("1234.56")` -> `"one thousand two hundred thirty-four dollars and fifty-six cents"`
- `Decimal("0.99")` -> `"zero dollars and ninety-nine cents"`

Amounts are rounded to cents first (half up, like everywhere else). Negative or too-large
amounts raise `ValueError`. Also add a line `Amount in words: ...` under the TOTAL line of the
rendered invoice, but only for USD invoices.
