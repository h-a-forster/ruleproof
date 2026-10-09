from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from invoicing import Invoice, LineItem


@pytest.fixture
def consulting_invoice() -> Invoice:
    return Invoice(
        number="INV-1001",
        customer="Acme GmbH",
        issue_date=date(2024, 3, 1),
        currency="EUR",
        items=[
            LineItem("Consulting", Decimal("3"), Decimal("120.00")),
            LineItem("Travel", Decimal("1"), Decimal("80.50"), "reduced"),
        ],
    )
