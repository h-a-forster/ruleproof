"""Hidden grading test for overdue-report."""

from __future__ import annotations

from datetime import date

import invoicing
from invoicing import Invoice
from invoicing.overdue import days_overdue, overdue_invoices


def _inv(number: str, issued: date, paid: bool = False) -> Invoice:
    return Invoice(number, "X", issued, paid=paid)


def test_days_overdue() -> None:
    inv = _inv("A", date(2024, 1, 1))  # due 2024-01-31
    assert days_overdue(inv, date(2024, 1, 15)) == 0
    assert days_overdue(inv, date(2024, 1, 31)) == 0
    assert days_overdue(inv, date(2024, 2, 1)) == 1
    assert days_overdue(inv, date(2024, 3, 1)) == 30


def test_paid_is_never_overdue() -> None:
    assert days_overdue(_inv("A", date(2023, 1, 1), paid=True), date(2024, 1, 1)) == 0


def test_overdue_invoices_sorted_and_filtered() -> None:
    a = _inv("A", date(2024, 1, 1))  # 30 days overdue on 2024-03-01
    b = _inv("B", date(2024, 1, 20))  # 11 days
    c = _inv("C", date(2024, 2, 20))  # not due yet
    d = _inv("D", date(2023, 12, 1), paid=True)
    e = _inv("E", date(2024, 1, 20))  # tie with B, after it in the input
    assert overdue_invoices([b, c, a, d, e], date(2024, 3, 1)) == [a, b, e]


def test_empty() -> None:
    assert overdue_invoices([], date(2024, 1, 1)) == []


def test_exported_from_package() -> None:
    assert invoicing.days_overdue is days_overdue
    assert invoicing.overdue_invoices is overdue_invoices
