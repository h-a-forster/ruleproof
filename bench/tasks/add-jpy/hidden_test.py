"""Hidden grading test for add-jpy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from invoicing import Invoice, LineItem, compute_totals, format_money, render_invoice, round_money


def test_format_and_round() -> None:
    assert format_money(Decimal("1234.5"), "JPY") == "¥1,235"
    assert format_money(Decimal("-12"), "JPY") == "-¥12"
    assert round_money(Decimal("10.5"), "JPY") == Decimal("11")


def test_invoice_in_yen() -> None:
    inv = Invoice(
        "JP-1",
        "Tanaka KK",
        date(2024, 4, 1),
        currency="JPY",
        items=[LineItem("Licence", Decimal("3"), Decimal("12500"))],
    )
    t = compute_totals(inv)
    assert t.subtotal == Decimal("37500")
    assert t.total == Decimal("45000")
    text = render_invoice(inv)
    assert "¥45,000" in text
    assert "¥45,000." not in text


def test_existing_currencies_unchanged() -> None:
    assert format_money(Decimal("1234.5"), "EUR") == "€1,234.50"
    assert format_money(Decimal("3"), "GBP") == "£3.00"
