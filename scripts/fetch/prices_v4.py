#!/usr/bin/env python3
"""Price layer v5: one qfq (forward-adjusted) series is the only truth.

Why this module exists
----------------------
Schema v4 stored unadjusted prices plus a per-session adjustment factor and
derived every adjusted price from them (hfq/qfq are monotone rescalings of raw).
This project's downstream -- compute_market_winners / screen_threshold_gainers /
structure_waves / emotion_rallies -- consumes only *ratios*: the 10-day return
and the limit-up test are both one-day close ratios, in which the adjustment
basis cancels. A qfq series therefore reproduces every downstream number
bit-for-bit while removing the two legs (raw + hfq) that v4 had to fetch and
reconcile per session.

So v5 fetches qfq once (adjust="qfq") and stores exactly that, in one file per
symbol. The factor/factor-event machinery is gone: there is no second leg to
reconcile and no anchor to drift.

Precision contract
------------------
Upstream prints qfq prices with two decimals. Downstream only ever divides one
qfq close by another (return / limit-up), so the two-decimal quantization is a
shared, exact property of both numerator and denominator -- ratios are exact to
their printed precision. There is no longer a multiply-then-quantize step that
could lose a tick.

Missing data is never invented: a session without a qfq close yields null prices
rather than a carried-forward synthetic value.
"""
from __future__ import annotations

import json
import math
import os
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 5

#: Upstream prints prices with two decimals.
TICK = Decimal("0.01")

PRICE_FIELDS = ("open", "high", "low", "close")

STORE_DIR = "price_store"


# --------------------------------------------------------------------------
# numeric helpers
# --------------------------------------------------------------------------
def quantize(value: Any) -> "float | None":
    """Round a price to the upstream tick using half-up (not banker's) rounding."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return float(Decimal(repr(number)).quantize(TICK, rounding=ROUND_HALF_UP))


def as_float(value: Any) -> "float | None":
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sorted_rows(rows: Iterable[dict[str, Any]], key: str = "trade_date") -> list[dict[str, Any]]:
    usable = [row for row in rows if isinstance(row, dict) and row.get(key)]
    return sorted(usable, key=lambda row: str(row[key]))


# --------------------------------------------------------------------------
# store (authority layer): one file per symbol holding the qfq series
# --------------------------------------------------------------------------
def build_store(
    ts_code: str,
    list_date: "str | None",
    qfq_rows: list[dict[str, Any]],
    price_mode: "str | None" = None,
    last_update: "str | None" = None,
    requested_first: "str | None" = None,
    requested_last: "str | None" = None,
) -> dict[str, Any]:
    """Fold a symbol's qfq series into ``price_store/<ts_code>.json``.

    v5 keeps exactly one series. ``is_limit_up`` is a derived field recomputed on
    every write (see ``fetch_akshare_data.annotate_limit_up``), not a network
    source. ``last_update`` is the ISO timestamp of this write and drives the
    cache-refresh "is today's bar already captured" gate.
    """
    qfq_out: list[dict[str, Any]] = []
    for row in _sorted_rows(qfq_rows):
        entry = {
            "trade_date": str(row["trade_date"]),
            "open": quantize(row.get("open")),
            "high": quantize(row.get("high")),
            "low": quantize(row.get("low")),
            "close": quantize(row.get("close")),
            "is_limit_up": row.get("is_limit_up"),
        }
        # volume / amount are optional; kept when the adapter saw them so the
        # derived artifacts can carry them without re-fetching.
        for extra in ("vol", "amount"):
            value = as_float(row.get(extra))
            if value is not None:
                entry[extra] = value
        qfq_out.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "ts_code": ts_code,
        "list_date": list_date,
        "price_mode": price_mode or "akshare_qfq",
        "basis": "qfq_direct",
        "definition": "qfq fetched directly; downstream consumes ratios only",
        "last_update": last_update,
        # Requested coverage is distinct from observed K-line coverage. A stock may
        # list after the window begins or be suspended on a requested session; those
        # dates were still checked and must not trigger the same network request on
        # every cache hit. No synthetic K-line is created for such dates.
        "requested_first": requested_first,
        "requested_last": requested_last,
        "qfq": qfq_out,
    }


def store_path(cache_dir: Path, ts_code: str) -> Path:
    return cache_dir / STORE_DIR / f"{ts_code}.json"


def read_store(cache_dir: Path, ts_code: str) -> "dict[str, Any] | None":
    payload = load_json(store_path(cache_dir, ts_code))
    if not isinstance(payload, dict):
        return None
    # a v5 store must carry a qfq list; a legacy/partial file is not usable
    if not isinstance(payload.get("qfq"), list):
        return None
    return payload


def write_store(cache_dir: Path, store: dict[str, Any]) -> None:
    atomic_write_json(store_path(cache_dir, store["ts_code"]), store)


def list_symbols(cache_dir: Path) -> list[str]:
    directory = cache_dir / STORE_DIR
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.json") if path.stat().st_size > 2)


def cache_range(store: dict[str, Any]) -> "tuple[str | None, str | None]":
    """Return (first, last) cached qfq trading sessions, or (None, None) if empty."""
    dates = [str(row["trade_date"]) for row in store.get("qfq") or [] if row.get("trade_date")]
    if not dates:
        return None, None
    ordered = sorted(dates)
    return ordered[0], ordered[-1]


# --------------------------------------------------------------------------
# derivation (store -> every consumer shape)
# --------------------------------------------------------------------------
def derive_rows(store: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Project one symbol's qfq store onto the consumer shapes.

    Returns ``{"raw": [...], "hfq": [...], "qfq": [...], "view": [...]}``. In v5
    there is a single series, so ``raw`` / ``hfq`` / ``qfq`` are all the qfq
    series (downstream only consumes ratios and ``is_limit_up``, which are
    basis-independent). The shapes are unchanged from v4 so compute/ and report/
    are untouched.
    """
    ts_code = store.get("ts_code")
    price_mode = store.get("price_mode") or "akshare_qfq"

    raw_rows: list[dict[str, Any]] = []
    hfq_rows: list[dict[str, Any]] = []
    qfq_rows: list[dict[str, Any]] = []
    view_rows: list[dict[str, Any]] = []
    for row in _sorted_rows(store.get("qfq") or []):
        day = str(row["trade_date"])
        prices = {field: quantize(row.get(field)) for field in PRICE_FIELDS}
        is_limit_up = row.get("is_limit_up")
        raw_rows.append({
            "ts_code": ts_code,
            "trade_date": day,
            **prices,
            "is_limit_up": is_limit_up,
        })
        hfq_rows.append({
            "ts_code": ts_code,
            "trade_date": day,
            **prices,
            "price_mode": price_mode,
        })
        qfq_rows.append({"ts_code": ts_code, "trade_date": day, **prices})
        view_rows.append({
            "ts_code": ts_code,
            "trade_date": day,
            # v5: every basis equals qfq, so the folded view carries one set of numbers.
            "hfq_open": prices["open"],
            "hfq_high": prices["high"],
            "hfq_low": prices["low"],
            "hfq_close": prices["close"],
            "qfq_open": prices["open"],
            "qfq_high": prices["high"],
            "qfq_low": prices["low"],
            "qfq_close": prices["close"],
            "raw_open": prices["open"],
            "raw_high": prices["high"],
            "raw_low": prices["low"],
            "raw_close": prices["close"],
            "is_limit_up": is_limit_up,
            "price_mode": price_mode,
        })
    return {"raw": raw_rows, "hfq": hfq_rows, "qfq": qfq_rows, "view": view_rows}


# --------------------------------------------------------------------------
# io helpers
# --------------------------------------------------------------------------
def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    os.replace(tmp, path)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
