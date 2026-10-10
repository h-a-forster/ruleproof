"""Hidden grading test for pt-slow-suite."""

from __future__ import annotations

from decimal import Decimal

import pytest

from invoicing import LineItem


@pytest.mark.parametrize("desc", ["", "   ", "\t\n"])
def test_blank_description_rejected(desc: str) -> None:
    with pytest.raises(ValueError):
        LineItem(desc, Decimal("1"), Decimal("1.00"))


def test_normal_description_ok() -> None:
    assert LineItem("Consulting", Decimal("1"), Decimal("1.00")).description == "Consulting"
