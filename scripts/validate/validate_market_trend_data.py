#!/usr/bin/env python3
"""Validate normalized market trend JSON without changing it."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate market trend data")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--expected-dates", type=int)
    parser.add_argument("--expected-markets", type=int)
    args = parser.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))
    dates = data.get("dates", [])
    markets = data.get("markets", [])
    winners = data.get("winners", [])
    errors: list[str] = []
    warnings: list[str] = []

    if len(dates) != len(set(dates)):
        errors.append("dates contains duplicates")
    for item in dates:
        try:
            date.fromisoformat(item)
        except (TypeError, ValueError):
            errors.append(f"invalid ISO date: {item}")
    if args.expected_dates is not None and len(dates) != args.expected_dates:
        errors.append(f"date count {len(dates)} != expected {args.expected_dates}")
    if args.expected_markets is not None and len(markets) != args.expected_markets:
        errors.append(f"market count {len(markets)} != expected {args.expected_markets}")

    keys = [(row.get("date"), row.get("market")) for row in winners]
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    if duplicates:
        errors.append(f"duplicate date-market winners: {duplicates[:5]}")
    for row in winners:
        required = ("date", "market", "code", "name", "pct")
        missing = [key for key in required if row.get(key) in (None, "")]
        if missing:
            errors.append(f"missing {missing} in row {row}")
        if row.get("market") not in markets:
            errors.append(f"unknown market {row.get('market')}")
        try:
            float(row.get("pct"))
        except (TypeError, ValueError):
            errors.append(f"non-numeric pct in row {row}")
        if row.get("new") is True or row.get("st") is True:
            errors.append(f"ineligible winner {row.get('code')}")

    expected = len(dates) * len(markets)
    if len(winners) < expected:
        warnings.append(f"missing combinations: {expected - len(winners)}; do not fill with zero")
    if not data.get("source") and not data.get("source_pages"):
        warnings.append("source metadata is missing")
    if data.get("warning"):
        warnings.append(str(data["warning"]))

    result = {"valid": not errors, "errors": errors, "warnings": warnings, "dates": len(dates), "markets": len(markets), "winners": len(winners)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
