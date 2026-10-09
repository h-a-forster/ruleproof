"""Hidden grading test for amount-in-words."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

import invoicing
from invoicing import Invoice, LineItem, render_invoice
from invoicing.words import amount_in_words


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0", "zero dollars"),
        ("1", "one dollar"),
        ("1.01", "one dollar and one cent"),
        ("21.50", "twenty-one dollars and fifty cents"),
        ("1234.56", "one thousand two hundred thirty-four dollars and fifty-six cents"),
        ("0.99", "zero dollars and ninety-nine cents"),
        ("100", "one hundred dollars"),
        ("1000000", "one million dollars"),
        ("2005019.10", "two million five thousand nineteen dollars and ten cents"),
        (
            "999999999.99",
            "nine hundred ninety-nine million nine hundred ninety-nine thousand "
            "nine hundred ninety-nine dollars and ninety-nine cents",
        ),
        ("12.005", "twelve dollars and one cent"),
        ("40.10", "forty dollars and ten cents"),
    ],
)
def test_amount_in_words(amount: str, expected: str) -> None:
    assert amount_in_words(Decimal(amount)) == expected


@pytest.mark.parametrize("amount", ["-1", "1000000000"])
def test_out_of_range(amount: str) -> None:
    with pytest.raises(ValueError):
        amount_in_words(Decimal(amount))


def test_exported() -> None:
    assert invoicing.amount_in_words is amount_in_words


def _inv(currency: str) -> Invoice:
    return Invoice(
        "US-1",
        "Acme Inc",
        date(2024, 5, 1),
        currency=currency,
        items=[LineItem("Support", Decimal("1"), Decimal("100.00"), "zero")],
    )


def test_render_usd_has_words() -> None:
    lines = render_invoice(_inv("USD")).splitlines()
    total_at = next(i for i, line in enumerate(lines) if line.startswith("TOTAL"))
    assert any("Amount in words: one hundred dollars" in line for line in lines[total_at + 1 :])


def test_render_eur_has_no_words() -> None:
    assert "Amount in words" not in render_invoice(_inv("EUR"))
