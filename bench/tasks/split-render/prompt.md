`render_invoice` in `render.py` has grown into one long function and we're about to add more
layouts. Please split it into three helpers in the same module, each returning a list of lines:

- `render_header(invoice) -> list[str]`: the invoice number, customer and dates block,
- `render_lines(invoice) -> list[str]`: the table of line items,
- `render_totals(invoice) -> list[str]`: subtotal, discount, tax, total and the payment note,

and make `render_invoice` just join them. The rendered output must stay exactly the same.
