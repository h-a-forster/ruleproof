from __future__ import annotations

from decimal import Decimal

import pytest

from invoicing.money import format_money, round_money


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("1.005", "1.01"), ("1.004", "1.00"), ("2.5", "2.50"), ("-1.005", "-1.01")],
)
def test_round_money_half_up(amount: str, expected: str) -> None:
    assert round_money(Decimal(amount), "EUR") == Decimal(expected)


def test_format_money_thousands_separator() -> None:
    assert format_money(Decimal("1234.5"), "EUR") == "€1,234.50"


def test_format_money_negative() -> None:
    assert format_money(Decimal("-3"), "USD") == "-$3.00"
