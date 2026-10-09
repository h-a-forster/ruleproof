"""Subtotal, discount, tax and total for an invoice."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from invoicing.generated import TAX_RATES
from invoicing.models import Invoice, LineItem
from invoicing.money import round_money

logger = logging.getLogger(__name__)

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class InvoiceTotals:
    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    total: Decimal


def line_total(item: LineItem, currency: str) -> Decimal:
    return round_money(item.quantity * item.unit_price, currency)


def compute_totals(invoice: Invoice) -> InvoiceTotals:
    """Totals for ``invoice``.

    Each line is rounded first. The discount applies to the whole invoice, and tax is due on
    the discounted amount of each line.
    """
    cur = invoice.currency
    lines = [(item, line_total(item, cur)) for item in invoice.items]
    subtotal = sum((amount for _, amount in lines), Decimal(0))
    discount = round_money(subtotal * invoice.discount_pct / HUNDRED, cur)

    tax = Decimal(0)
    for item, amount in lines:
        tax += amount * TAX_RATES[item.tax_category]
    tax = round_money(tax, cur)

    total = subtotal - discount + tax
    logger.debug("invoice %s: subtotal=%s total=%s", invoice.number, subtotal, total)
    return InvoiceTotals(subtotal=subtotal, discount=discount, tax=tax, total=total)
