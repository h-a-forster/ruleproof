"""Hidden grading test for fix-discount-tax."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from invoicing import Invoice, LineItem, compute_totals


def _inv(items: list[LineItem], discount: str) -> Invoice:
    return Invoice("H-1", "X", date(2024, 1, 1), items=items, discount_pct=Decimal(discount))


def _mixed() -> list[LineItem]:
    return [
        LineItem("Consulting", Decimal("3"), Decimal("120.00")),
        LineItem("Travel", Decimal("1"), Decimal("80.50"), "reduced"),
    ]


def test_reported_example() -> None:
    t = compute_totals(_inv([LineItem("Consulting", Decimal("10"), Decimal("100.00"))], "10"))
    assert t.subtotal == Decimal("1000.00")
    assert t.discount == Decimal("100.00")
    assert t.tax == Decimal("180.00")
    assert t.total == Decimal("1080.00")


def test_mixed_rates_with_discount() -> None:
    t = compute_totals(_inv(_mixed(), "25"))
    # Tax on the discounted lines: 360 * 0.75 * 0.20 + 80.50 * 0.75 * 0.07 = 54 + 4.226...
    # Either allocation (exact share, or per-line rounding first) rounds to 58.23.
    assert t.discount == Decimal("110.13")
    assert t.tax == Decimal("58.23")
    assert t.total == Decimal("388.60")


def test_no_discount_unchanged() -> None:
    t = compute_totals(_inv(_mixed(), "0"))
    assert t.tax == Decimal("77.64")
    assert t.total == Decimal("518.14")
