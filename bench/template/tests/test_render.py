from __future__ import annotations

from invoicing import Invoice, render_invoice


def test_render_contains_totals(consulting_invoice: Invoice) -> None:
    text = render_invoice(consulting_invoice)
    assert "INVOICE INV-1001" in text
    assert "€518.14" in text
    assert "Please pay by 2024-03-31." in text


def test_render_paid(consulting_invoice: Invoice) -> None:
    consulting_invoice.paid = True
    assert "PAID - thank you." in render_invoice(consulting_invoice)


def test_lines_fit_width(consulting_invoice: Invoice) -> None:
    for line in render_invoice(consulting_invoice).splitlines():
        assert len(line) <= 60
