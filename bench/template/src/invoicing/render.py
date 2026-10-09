"""Plain-text invoice layout used in emails."""

from __future__ import annotations

from invoicing.models import Invoice
from invoicing.money import format_money
from invoicing.totals import compute_totals, line_total

WIDTH = 60


def render_invoice(invoice: Invoice) -> str:
    cur = invoice.currency
    out: list[str] = []
    out.append(f"INVOICE {invoice.number}".ljust(WIDTH - 22) + f"Issued: {invoice.issue_date}")
    out.append(f"Bill to: {invoice.customer}".ljust(WIDTH - 22) + f"Due:    {invoice.due_date}")
    out.append("=" * WIDTH)
    out.append(f"{'Description':<28}{'Qty':>6}{'Unit':>12}{'Amount':>14}")
    out.append("-" * WIDTH)
    for item in invoice.items:
        desc = item.description if len(item.description) <= 27 else item.description[:24] + "..."
        qty = f"{item.quantity.normalize():f}"
        unit = format_money(item.unit_price, cur)
        amount = format_money(line_total(item, cur), cur)
        out.append(f"{desc:<28}{qty:>6}{unit:>12}{amount:>14}")
    out.append("-" * WIDTH)
    totals = compute_totals(invoice)
    out.append(f"{'Subtotal':<46}{format_money(totals.subtotal, cur):>14}")
    if totals.discount:
        label = f"Discount ({invoice.discount_pct.normalize():f}%)"
        out.append(f"{label:<46}{format_money(-totals.discount, cur):>14}")
    out.append(f"{'Tax':<46}{format_money(totals.tax, cur):>14}")
    out.append("=" * WIDTH)
    out.append(f"{'TOTAL':<46}{format_money(totals.total, cur):>14}")
    if invoice.paid:
        out.append("")
        out.append("PAID - thank you.")
    else:
        out.append("")
        out.append(f"Please pay by {invoice.due_date}.")
    return "\n".join(out) + "\n"
