"""Hidden grading test for split-render."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from invoicing import Invoice, LineItem
from invoicing import render as render_mod

EXPECTED_A = (
    "INVOICE INV-2001                      Issued: 2024-03-01\n"
    "Bill to: Acme GmbH                    Due:    2024-03-31\n"
    "============================================================\n"
    "Description                    Qty        Unit        Amount\n"
    "------------------------------------------------------------\n"
    "Consulting                       3     €120.00       €360.00\n"
    "Travel to the customer s...    1.5      €80.50       €120.75\n"
    "------------------------------------------------------------\n"
    "Subtotal                                             €480.75\n"
    "Tax                                                   €80.45\n"
    "============================================================\n"
    "TOTAL                                                €561.20\n"
    "\n"
    "Please pay by 2024-03-31.\n"
)

EXPECTED_B = (
    "INVOICE INV-2002                      Issued: 2024-02-10\n"
    "Bill to: Blue Ltd                     Due:    2024-02-24\n"
    "============================================================\n"
    "Description                    Qty        Unit        Amount\n"
    "------------------------------------------------------------\n"
    "Licence                          2   £1,999.99     £3,999.98\n"
    "------------------------------------------------------------\n"
    "Subtotal                                           £3,999.98\n"
    "Discount (12.5%)                                    -£500.00\n"
    "Tax                                                    £0.00\n"
    "============================================================\n"
    "TOTAL                                              £3,499.98\n"
    "\n"
    "PAID - thank you.\n"
)


def _a() -> Invoice:
    return Invoice(
        "INV-2001",
        "Acme GmbH",
        date(2024, 3, 1),
        currency="EUR",
        items=[
            LineItem("Consulting", Decimal("3"), Decimal("120.00")),
            LineItem(
                "Travel to the customer site in Hamburg",
                Decimal("1.5"),
                Decimal("80.50"),
                "reduced",
            ),
        ],
    )


def _b() -> Invoice:
    return Invoice(
        "INV-2002",
        "Blue Ltd",
        date(2024, 2, 10),
        currency="GBP",
        items=[LineItem("Licence", Decimal("2"), Decimal("1999.99"), "zero")],
        discount_pct=Decimal("12.5"),
        payment_terms_days=14,
        paid=True,
    )


@pytest.mark.parametrize(("make", "expected"), [(_a, EXPECTED_A), (_b, EXPECTED_B)])
def test_output_unchanged(make: object, expected: str) -> None:
    assert render_mod.render_invoice(make()) == expected  # type: ignore[operator]


@pytest.mark.parametrize("make", [_a, _b])
def test_helpers_compose(make: object) -> None:
    inv = make()  # type: ignore[operator]
    header = render_mod.render_header(inv)
    lines = render_mod.render_lines(inv)
    totals = render_mod.render_totals(inv)
    for part in (header, lines, totals):
        assert isinstance(part, list)
        assert all(isinstance(s, str) for s in part)
    joined = "\n".join(header + lines + totals)
    assert render_mod.render_invoice(inv).rstrip("\n") == joined.rstrip("\n")
    assert header[0].startswith("INVOICE ")
    assert any(s.startswith("Licence") or s.startswith("Consulting") for s in lines)
    assert any(s.startswith("TOTAL") for s in totals)
    assert not any(s.startswith("TOTAL") for s in header + lines)
    assert not any(s.startswith("Bill to:") for s in lines + totals)
