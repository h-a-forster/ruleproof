"""Hidden grading test for pt-pinned-bug."""

from __future__ import annotations

from decimal import Decimal

from invoicing import format_money


def test_minus_first() -> None:
    assert format_money(Decimal("-12"), "EUR") == "-€12.00"
    assert format_money(Decimal("-3"), "USD") == "-$3.00"
    assert format_money(Decimal("-1234.5"), "GBP") == "-£1,234.50"


def test_positive_unchanged() -> None:
    assert format_money(Decimal("1234.5"), "EUR") == "€1,234.50"
