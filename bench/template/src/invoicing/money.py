"""Rounding and formatting of money amounts."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from invoicing.generated import CURRENCIES


def quantum(currency: str) -> Decimal:
    """The smallest unit of ``currency`` (``Decimal('0.01')`` for two decimals)."""
    return Decimal(1).scaleb(-CURRENCIES[currency].decimals)


def round_money(amount: Decimal, currency: str) -> Decimal:
    """Round half up to the currency's minor unit, as our accounting system does."""
    return amount.quantize(quantum(currency), rounding=ROUND_HALF_UP)


def format_money(amount: Decimal, currency: str) -> str:
    """Format ``amount`` for display, e.g. ``€1,234.50`` or ``-$3.00``."""
    c = CURRENCIES[currency]
    rounded = round_money(amount, currency)
    sign = "-" if rounded < 0 else ""
    return f"{sign}{c.symbol}{abs(rounded):,.{c.decimals}f}"
