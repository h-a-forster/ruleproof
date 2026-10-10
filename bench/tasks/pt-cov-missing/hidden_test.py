"""Hidden grading test for pt-cov-missing."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from invoicing import Invoice, LineItem, render_invoice


def _inv(**kw: object) -> Invoice:
    return Invoice(
        "H-1", "Acme", date(2024, 3, 1), items=[LineItem("A", Decimal("1"), Decimal("10"))], **kw
    )


def test_po_line_below_bill_to() -> None:
    lines = render_invoice(_inv(po_number="4500012345")).splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("Bill to: Acme"))
    assert lines[i + 1].strip().startswith("PO: 4500012345")
    assert all(len(line) <= 60 for line in lines)


def test_no_po_unchanged() -> None:
    text = render_invoice(_inv())
    assert "PO:" not in text
    assert _inv().po_number is None
