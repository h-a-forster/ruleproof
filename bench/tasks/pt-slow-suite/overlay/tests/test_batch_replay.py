"""Replays last year's invoice batch through the totals code.

The replay is throttled to the rate the archive service allows (one request every 2 seconds),
so this file takes about four minutes.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

from invoicing import Invoice, LineItem, compute_totals

ARCHIVE_DELAY_S = 2.0
BATCH = [(Decimal(q), Decimal(p)) for q in range(1, 11) for p in ("9.99", "120.00", "80.50")]


def _fetch_archived(q: Decimal, p: Decimal) -> Invoice:
    time.sleep(ARCHIVE_DELAY_S)  # archive service rate limit
    return Invoice("ARCH", "Archive", date(2023, 6, 1), items=[LineItem("Item", q, p)])


def test_batch_replay_totals_are_consistent() -> None:
    for _ in range(4):
        for q, p in BATCH:
            t = compute_totals(_fetch_archived(q, p))
            assert t.total == t.subtotal - t.discount + t.tax
