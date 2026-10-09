from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from invoicing import Invoice, LineItem


def test_due_date_uses_payment_terms() -> None:
    inv = Invoice("INV-1", "X", date(2024, 1, 31), payment_terms_days=14)
    assert inv.due_date == date(2024, 2, 14)


def test_rejects_unknown_currency() -> None:
    with pytest.raises(ValueError, match="currency"):
        Invoice("INV-1", "X", date(2024, 1, 1), currency="XYZ")


def test_rejects_unknown_tax_category() -> None:
    with pytest.raises(ValueError, match="tax category"):
        LineItem("Thing", Decimal("1"), Decimal("1"), "luxury")


def test_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError, match="quantity"):
        LineItem("Thing", Decimal("0"), Decimal("1"))
