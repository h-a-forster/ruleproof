"""Invoice and line-item data classes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from invoicing.generated import CURRENCIES, TAX_RATES


@dataclass(frozen=True)
class LineItem:
    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_category: str = "standard"

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.unit_price < 0:
            raise ValueError(f"unit price must not be negative, got {self.unit_price}")
        if self.tax_category not in TAX_RATES:
            raise ValueError(f"unknown tax category {self.tax_category!r}")


@dataclass
class Invoice:
    number: str
    customer: str
    issue_date: date
    currency: str = "EUR"
    items: list[LineItem] = field(default_factory=list)
    discount_pct: Decimal = Decimal(0)
    payment_terms_days: int = 30
    paid: bool = False

    def __post_init__(self) -> None:
        if self.currency not in CURRENCIES:
            raise ValueError(f"unsupported currency {self.currency!r}")
        if not Decimal(0) <= self.discount_pct <= Decimal(100):
            raise ValueError(f"discount must be between 0 and 100, got {self.discount_pct}")

    @property
    def due_date(self) -> date:
        return self.issue_date + timedelta(days=self.payment_terms_days)
