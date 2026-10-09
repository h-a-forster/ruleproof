# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## 0.4.0 - 2024-02-12

### Added

- `render_invoice` prints the due date under the issue date.
- GBP in the currency table.

### Fixed

- Line totals are rounded per line before summing, matching what customers see.

## 0.3.0 - 2023-11-30

### Added

- Per-invoice percentage discounts (`Invoice.discount_pct`).
- Reduced and zero tax categories.
