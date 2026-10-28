#!/usr/bin/env python3
"""Repair the derived ``is_limit_up`` field across an existing schema-v4 cache.

Why this exists
---------------
``is_limit_up`` is a *derived* field: it is computed from the K-line (adjusted
close vs the previous session's adjusted close) and must therefore agree with a
recomputation of the data that was actually persisted. Before 2026-10-26 the
annotation ran once inside the fetch attempt, on in-memory rows. When a session's
factor was still missing at that moment, ``annotate_limit_up`` walked back to an
earlier predecessor for ``prev_close`` and silently turned a borderline limit-up
into ``False``. ``build_store`` then normalised the payload, so the stored mark
disagreed with its own data -- flagged by ``validate_cache_contract.py`` as
``limit_up_field_inconsistent``.

Measured on 2026-10-26 (same 45-session window, full market):
  * single-process build:  19 wrong rows out of 249,829 (0.008%)
  * fresh 8-shard rebuild: 32 wrong rows
  * a resume-built cache:   0 (it never re-derives from network)

The fetcher now recomputes the mark on the finalised payload before writing
(``refresh_limit_up_in_payload``), so new runs are correct by construction. This
script repairs caches produced before that fix; it is idempotent and only touches
``is_limit_up``.

Usage
-----
    python scripts/fetch/repair_limit_up.py --cache-dir ./cache/akshare --dry-run
    python scripts/fetch/repair_limit_up.py --cache-dir ./cache/akshare
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_akshare_data as fad  # noqa: E402  (path bootstrap must run first)


def main() -> int:
    parser = argparse.ArgumentParser(description="Recompute is_limit_up in an existing cache")
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="report flips without writing")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    store_dir = args.cache_dir / fad.prices_v4.STORE_DIR
    if not store_dir.is_dir():
        raise SystemExit(f"no {fad.prices_v4.STORE_DIR}/ under {args.cache_dir}")

    files = sorted(store_dir.glob("*.json"))
    changed_rows = 0
    changed_symbols: list[str] = []
    details: list[dict[str, object]] = []
    failures: list[str] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        before = {
            row.get("trade_date"): row.get("is_limit_up")
            for row in payload.get("qfq") or []
            if isinstance(row, dict) and row.get("trade_date")
        }
        flips = fad.refresh_limit_up_in_payload(payload, payload.get("ts_code") or path.stem)
        if not flips:
            continue
        changed_rows += flips
        changed_symbols.append(payload.get("ts_code") or path.stem)
        for row in payload.get("qfq") or []:
            day = row.get("trade_date")
            if day and row.get("is_limit_up") != before.get(day):
                details.append({
                    "ts_code": payload.get("ts_code") or path.stem,
                    "trade_date": day,
                    "was": before.get(day),
                    "now": row.get("is_limit_up"),
                })
        if not args.dry_run:
            fad.prices_v4.write_store(args.cache_dir, payload)

    report = {
        "cache_dir": str(args.cache_dir),
        "dry_run": args.dry_run,
        "symbols_scanned": len(files),
        "symbols_changed": len(changed_symbols),
        "rows_changed": changed_rows,
        "changed_sample": changed_symbols[:10],
        "details": details[:50],
        "failures": failures,
    }
    if args.json_out:
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
