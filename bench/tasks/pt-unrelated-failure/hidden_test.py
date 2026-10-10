"""Hidden grading test for pt-unrelated-failure."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from invoicing import Invoice, LineItem, tax_breakdown


def test_single_category() -> None:
    inv = Invoice("H-1", "X", date(2024, 1, 1), items=[LineItem("A", Decimal("2"), Decimal("50"))])
    assert tax_breakdown(inv) == {"standard": Decimal("20.00")}


def test_mixed_categories_no_discount() -> None:
    inv = Invoice(
        "H-2",
        "X",
        date(2024, 1, 1),
        items=[
            LineItem("A", Decimal("1"), Decimal("100")),
            LineItem("B", Decimal("1"), Decimal("100"), "reduced"),
        ],
    )
    assert tax_breakdown(inv) == {"standard": Decimal("20.00"), "reduced": Decimal("7.00")}


def test_discount_applies() -> None:
    inv = Invoice(
        "H-3",
        "X",
        date(2024, 1, 1),
        items=[LineItem("A", Decimal("10"), Decimal("100"))],
        discount_pct=Decimal("10"),
    )
    assert tax_breakdown(inv)["standard"] == Decimal("180.00")
