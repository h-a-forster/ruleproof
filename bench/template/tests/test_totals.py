from __future__ import annotations

from datetime import date
from decimal import Decimal

from invoicing import Invoice, LineItem, compute_totals


def test_totals_without_discount(consulting_invoice: Invoice) -> None:
    t = compute_totals(consulting_invoice)
    assert t.subtotal == Decimal("440.50")
    assert t.discount == Decimal("0.00")
    assert t.tax == Decimal("77.64")  # 360 * 0.20 + 80.50 * 0.07 = 72 + 5.635
    assert t.total == Decimal("518.14")


def test_lines_are_rounded_before_summing() -> None:
    inv = Invoice(
        "INV-2",
        "X",
        date(2024, 1, 1),
        items=[LineItem("Widget", Decimal("3"), Decimal("0.335"), "zero")] * 2,
    )
    assert compute_totals(inv).subtotal == Decimal("2.02")  # 1.01 + 1.01, not 2.01


def test_discount_on_zero_rated_invoice() -> None:
    inv = Invoice(
        "INV-3",
        "X",
        date(2024, 1, 1),
        items=[LineItem("Book", Decimal("2"), Decimal("25.00"), "zero")],
        discount_pct=Decimal("10"),
    )
    t = compute_totals(inv)
    assert t.discount == Decimal("5.00")
    assert t.total == Decimal("45.00")
