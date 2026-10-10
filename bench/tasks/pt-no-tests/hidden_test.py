"""Hidden grading test for pt-no-tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from invoicing import Invoice, LineItem, render_invoice


def _inv(paid: bool) -> Invoice:
    return Invoice(
        "H-1", "X", date(2024, 3, 1), items=[LineItem("A", Decimal("1"), Decimal("10"))], paid=paid
    )


def test_unpaid_footer() -> None:
    text = render_invoice(_inv(False))
    assert "Payment due by 2024-03-31." in text
    assert "Please pay by" not in text


def test_paid_footer_unchanged() -> None:
    assert "PAID - thank you." in render_invoice(_inv(True))
