#!/usr/bin/env python3
"""NeoData MCP raw envelopes -> schema v5 qfq cache contract.

Why this layer exists
---------------------
MCP tools are only callable from the agent loop, never from a normal Python
process. So the skill cannot let a script call NeoData directly; the agent has
to do the calling. The tempting shortcut is to let the agent also write the
contract files -- but then determinism moves inside an LLM: the same request can
produce a slightly different cache on every run, and ``validate_cache_contract``
would be checking the model's typing rather than the pipeline.

This module draws the line:

    agent  : call the MCP, land the response verbatim, nothing else
    script : parse, filter, unit-convert, write the contract

Same envelopes in -> same cache out, byte for byte.

Envelope contract
-----------------
The agent writes one JSON file per MCP call into ``--raw-dir``. The file name
is free (only ``*.json`` matters), so the agent never has to encode semantics
in a path. Every envelope is::

    {
      "tool": "trade_calendar" | "quote_and_kline" | "screen_cn_stocks",
      "purpose": "calendar" | "kline" | "limit_up" | "master",
      "params": {"codes": "...", "adjust_type": "0", ...},
      "fetched_at": "2026-10-26T01:49:42+08:00",
      "trace_id": "<MCP metadata.trace_id>",
      "result": [ ... MCP ``result`` field, verbatim ... ]
    }

``purpose`` is required for ``screen_cn_stocks`` (the same tool serves both the
limit-up pool and the stock master) and optional elsewhere, where the tool name
already decides it.

Why NeoData is a good fit for v5
--------------------------------
v5 made ``price_store/<ts>.json`` a single qfq (forward-adjusted) series -- the
factor / anchor machinery is gone, and the qfq four-prices are computed once at
ingestion from ``factor = hfq_close / raw_close`` (anchor = last factor, so the
latest bar's qfq equals its raw, matching akshare's qfq anchor). ``quote_and_kline``
returns exactly the raw/hfq pair when called twice per batch -- ``adjust_type=0``
for raw, ``adjust_type=2`` for hfq -- so the qfq is derived from the same two
printed closes the reference cache used, and nothing has to be chained or
re-based. AkShare needed a per-symbol ``--enrich-ohlc`` pass for the same
information.

Guard rails (a wrong cache is worse than no cache)
--------------------------------------------------
* A truncated result is refused, not stored: ``totalCount`` must equal the rows
  actually returned, otherwise the "full market" set is silently a page.
* A limit-up pool must prove itself: every row has to satisfy
  ``close == limit_up_price``. A threshold list (>=9.9%) must never be recorded
  as a pool, because the contract asserts the pool is authoritative.
* Suspended sessions ("未开盘") are dropped and counted, never carried forward.
* A symbol without an hfq side keeps ``factor`` empty and gets null adjusted
  prices downstream; a fabricated factor would invent a corporate-action-free
  history.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prices_v4 as P  # noqa: E402

A_SHARE_SUFFIX = (".SH", ".SZ", ".BJ")
BOARD_OF = (
    (("600", "601", "603", "605"), "沪市主板"),
    (("688", "689"), "科创板"),
    (("000", "001", "002", "003"), "深市主板"),
    (("300", "301"), "创业板"),
    (("430", "830", "870", "920"), "北交所"),
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMPTY_PRICE = {"", "-", "--", "未开盘", "None", "null"}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def normalize_date(value: Any) -> "str | None":
    """``20261023`` / ``2026-10-23T00:00`` / ``2026-10-23`` -> ``2026-10-23``."""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    text = text.replace("/", "-")
    head = text[:10]
    return head if DATE_RE.match(head) else None


def symbol6(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def board_of(symbol: str) -> "str | None":
    for prefixes, name in BOARD_OF:
        if symbol.startswith(prefixes):
            return name
    return None


def price_of(value: Any) -> "float | None":
    """Return ``None`` for a suspended print instead of raising or coercing to 0."""
    text = str(value if value is not None else "").strip()
    if text in EMPTY_PRICE:
        return None
    return P.as_float(text)


def _factor_of(hfq_close: Any, raw_close: Any) -> "float | None":
    """``hfq_close / raw_close`` -- the v4 adjustment factor -- or ``None``.

    v5 stores a single qfq series and keeps no factor, but the qfq four-prices are
    still computed from this ratio at ingestion time, so the result matches the
    qfq akshare produces (anchor = last factor). Returns ``None`` whenever either
    side is missing, non-numeric, or non-positive (e.g. a suspended print).
    """
    h = price_of(hfq_close)
    r = price_of(raw_close)
    if h is None or r is None or h <= 0 or r <= 0:
        return None
    return h / r


def _scale(value: Any, scale: float) -> "float | None":
    """``price_of(value) * scale``; ``None`` in, ``None`` out."""
    v = price_of(value)
    if v is None:
        return None
    return v * scale


def load_envelopes(raw_dir: Path) -> "tuple[list[dict[str, Any]], list[dict[str, Any]]]":
    """Read every ``*.json`` under ``raw_dir``; return (usable, rejected)."""
    usable: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if not raw_dir.is_dir():
        return usable, rejected
    for path in sorted(raw_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            rejected.append({"file": path.name, "reason": f"unreadable: {type(exc).__name__}"})
            continue
        if not isinstance(payload, dict):
            rejected.append({"file": path.name, "reason": "envelope is not an object"})
            continue
        tool = str(payload.get("tool") or "").strip()
        if tool not in ("trade_calendar", "quote_and_kline", "screen_cn_stocks"):
            rejected.append({"file": path.name, "reason": f"unsupported tool {tool!r}"})
            continue
        if not isinstance(payload.get("result"), list):
            rejected.append({"file": path.name, "reason": "envelope has no result[]"})
            continue
        payload["_file"] = path.name
        usable.append(payload)
    return usable, rejected


def purpose_of(envelope: dict[str, Any]) -> "str | None":
    declared = str(envelope.get("purpose") or "").strip()
    if declared:
        return declared
    tool = envelope.get("tool")
    if tool == "trade_calendar":
        return "calendar"
    if tool == "quote_and_kline":
        return "kline"
    return None


# --------------------------------------------------------------------------
# decoders
# --------------------------------------------------------------------------
def decode_calendar(envelopes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Open sessions per date, unioned across the A-share exchanges."""
    open_dates: set[str] = set()
    closed_dates: set[str] = set()
    upstream_dates: set[str] = set()
    for envelope in envelopes:
        for row in envelope.get("result") or []:
            if not isinstance(row, dict):
                continue
            day = normalize_date(row.get("trading_date") or row.get("cal_date"))
            if not day:
                continue
            market = str(row.get("market_code") or "").upper()
            if market and market not in ("SH", "SZ", "BJ"):
                continue
            upstream_dates.add(day)
            state = str(row.get("market_state") or row.get("is_open") or "").strip()
            is_open = not state.startswith("休市") if state and not state.isdigit() else bool(int(state or 0))
            (open_dates if is_open else closed_dates).add(day)
    return {
        "open_dates": sorted(open_dates - closed_dates),
        "closed_dates": sorted(closed_dates - open_dates),
        "upstream_dates": sorted(upstream_dates),
    }


def decode_kline(envelopes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Split ``quote_and_kline`` results by adjustment side.

    ``adjust_type`` 0 -> unadjusted four prices, 2 -> hfq close for the factor.
    ``1`` (qfq) is accepted but ignored: v4 derives qfq from raw and the anchor,
    and storing an upstream qfq would reintroduce the basis drift v4 removed.
    """
    raw: dict[str, dict[str, dict[str, Any]]] = {}
    hfq_close: dict[str, dict[str, float]] = {}
    names: dict[str, str] = {}
    markets: dict[str, str] = {}
    suspended = 0
    ignored_qfq = 0
    for envelope in envelopes:
        params = envelope.get("params") or {}
        adjust = str(params.get("adjust_type", "0")).strip()
        if adjust == "1":
            ignored_qfq += 1
            continue
        side = "hfq" if adjust == "2" else "raw"
        for item in envelope.get("result") or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip().upper()
            if not code or not code.endswith(A_SHARE_SUFFIX):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                names[code] = name
            if item.get("market_type"):
                markets[code] = str(item["market_type"])
            for bar in item.get("kline_list") or []:
                if not isinstance(bar, dict):
                    continue
                day = normalize_date(bar.get("trade_date") or bar.get("date"))
                if not day:
                    continue
                close = price_of(bar.get("close"))
                if close is None:
                    suspended += 1
                    continue
                if side == "hfq":
                    hfq_close.setdefault(code, {})[day] = close
                    continue
                raw.setdefault(code, {})[day] = {
                    "trade_date": day,
                    "open": price_of(bar.get("open")),
                    "high": price_of(bar.get("high")),
                    "low": price_of(bar.get("low")),
                    "close": close,
                }
    return {
        "raw": raw,
        "hfq_close": hfq_close,
        "names": names,
        "markets": markets,
        "suspended_rows": suspended,
        "ignored_qfq_envelopes": ignored_qfq,
    }


def parse_sql_text(payload: Any) -> tuple[list[str], list[dict[str, str]]]:
    """Pull the header and rows out of ``screen_cn_stocks``'s CSV-in-a-string."""
    blob = payload
    if isinstance(blob, dict):
        blob = blob.get("sqlResult", blob)
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            return [], []
    if isinstance(blob, dict):
        for block in blob.get("data") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return _parse_csv(block["text"])
        return [], []
    return [], []


def _parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return [], []
    header = next(csv.reader([lines[0]]))
    rows = []
    for line in lines[1:]:
        if line.strip().lower().startswith(("(", "—", "total", "rows in set")):
            continue
        values = next(csv.reader([line]))
        if len(values) != len(header):
            continue
        rows.append(dict(zip(header, values)))
    return header, rows


def decode_limit_up(envelopes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Assetize each session's limit-up set, refusing anything that is not a pool."""
    pools: dict[str, dict[str, Any]] = {}
    refusals: list[dict[str, Any]] = []
    for envelope in envelopes:
        params = envelope.get("params") or {}
        result = envelope.get("result") or []
        first = next((item for item in result if isinstance(item, dict)), None)
        if first is None:
            refusals.append({"file": envelope.get("_file"), "reason": "no result rows"})
            continue
        day = normalize_date(params.get("trade_date") or params.get("date"))
        if not day:
            # fall back to a date in the file name (limitup_2026-10-15.json)
            match = re.search(r"\d{4}-\d{2}-\d{2}|\d{8}", str(envelope.get("_file") or ""))
            day = normalize_date(match.group()) if match else None
        if not day:
            refusals.append({"file": envelope.get("_file"), "reason": "cannot resolve trade_date (set params.trade_date)"})
            continue
        header, rows = parse_sql_text(first)
        if not rows:
            refusals.append({"file": envelope.get("_file"), "day": day, "reason": "empty table"})
            continue
        total = first.get("totalCount")
        if isinstance(total, int) and total != len(rows):
            refusals.append({
                "file": envelope.get("_file"), "day": day,
                "reason": f"truncated page: totalCount={total} but {len(rows)} rows returned; "
                          "a partial page must never be stored as the session's pool",
            })
            continue
        code_col = next((name for name in header if "代码" in name or name.lower() in ("ts_code", "code")), None)
        close_col = next((name for name in header if "收盘" in name), None)
        limit_col = next((name for name in header if "涨停价" in name), None)
        if code_col is None or close_col is None or limit_col is None:
            refusals.append({
                "file": envelope.get("_file"), "day": day,
                "reason": f"cannot prove a limit-up set: need code/close/limit-price columns, got {header}",
            })
            continue
        codes: set[str] = set()
        unproven = []
        for row in rows:
            close = price_of(row.get(close_col))
            limit = price_of(row.get(limit_col))
            if close is None or limit is None or abs(close - limit) > 0.005:
                unproven.append(row.get(code_col))
                continue
            code6 = symbol6(row.get(code_col))
            if code6:
                codes.add(code6)
        if unproven:
            refusals.append({
                "file": envelope.get("_file"), "day": day,
                "reason": f"{len(unproven)} rows are not at their limit price (e.g. {unproven[:3]}); "
                          "a threshold list cannot be recorded as a pool",
            })
            continue
        pools[day] = {
            "codes": codes,
            "fetched_at": envelope.get("fetched_at") or datetime.now().astimezone().isoformat(timespec="seconds"),
            "trace_id": envelope.get("trace_id"),
            "source_tool": envelope.get("tool"),
            "source_query": first.get("sql") or (envelope.get("params") or {}).get("query"),
            "reasoning": (first.get("reasoning") or "").strip()[:500] or None,
        }
    return {"pools": pools, "refusals": refusals}


def decode_master(envelopes: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Stock master rows out of ``screen_cn_stocks`` (code / name / list date)."""
    out: dict[str, dict[str, Any]] = {}
    for envelope in envelopes:
        result = envelope.get("result") or []
        first = next((item for item in result if isinstance(item, dict)), None)
        if first is None:
            continue
        header, rows = parse_sql_text(first)
        if not rows:
            continue
        code_col = next((name for name in header if "代码" in name), None)
        name_col = next((name for name in header if "简称" in name or "名称" in name), None)
        list_col = next((name for name in header if "上市" in name), None)
        if code_col is None or name_col is None:
            continue
        for row in rows:
            code = str(row.get(code_col) or "").strip().upper()
            if not code.endswith(A_SHARE_SUFFIX):
                continue
            symbol = symbol6(code)
            out[code] = {
                "ts_code": code,
                "symbol": symbol,
                "name": str(row.get(name_col) or "").strip(),
                "list_date": normalize_date(row.get(list_col)) if list_col else None,
                "list_status": "L",
                "exchange": "SSE" if code.endswith(".SH") else ("BSE" if code.endswith(".BJ") else "SZSE"),
                "market": board_of(symbol),
            }
    return out


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def build_calendar(
    cache_dir: Path,
    calendar: dict[str, Any],
    kline_sessions: "set[str]",
    as_of: str,
) -> dict[str, Any]:
    """Persist ``calendar.json`` truncated at ``as_of`` plus the upstream table.

    Falls back to the union of observed kline sessions when no calendar envelope
    was landed. That fallback is a real session set, not a guess, but it only
    covers the window the agent actually fetched -- so it is marked partial
    rather than presented as the exchange calendar.
    """
    source = "trade_calendar"
    open_dates = [day for day in calendar["open_dates"] if day <= as_of]
    upstream = calendar["upstream_dates"]
    if not open_dates:
        source = "derived_from_kline_sessions"
        open_dates = sorted(day for day in kline_sessions if day <= as_of)
    payload = [{"cal_date": day, "is_open": 1} for day in open_dates]
    P.atomic_write_json(cache_dir / "calendar.json", payload)
    if upstream:
        P.atomic_write_json(
            cache_dir / "calendar_upstream.json",
            [{"cal_date": day, "is_open": 1 if day in set(calendar["open_dates"]) else 0} for day in upstream],
        )
    return {"source": source, "sessions": len(payload), "first": payload[0]["cal_date"] if payload else None,
            "last": payload[-1]["cal_date"] if payload else None}


def build_master(
    cache_dir: Path,
    master: dict[str, dict[str, Any]],
    names: dict[str, str],
    markets: dict[str, str],
) -> dict[str, Any]:
    """Persist ``stock_basic.json``; never invent a listing date.

    Downstream excludes a candidate it cannot date-check, which is the contract's
    intent. Filling a missing ``list_date`` with a plausible value would silently
    admit a new listing as a champion.
    """
    merged: dict[str, dict[str, Any]] = {}
    for code, row in master.items():
        merged[code] = dict(row)
    for code, name in names.items():
        if code in merged:
            merged[code].setdefault("name", name)
            continue
        symbol = symbol6(code)
        merged[code] = {
            "ts_code": code, "symbol": symbol, "name": name, "list_date": None,
            "list_status": "L",
            "exchange": "SSE" if code.endswith(".SH") else ("BSE" if code.endswith(".BJ") else "SZSE"),
            "market": board_of(symbol) or markets.get(code),
        }
    payload = sorted(merged.values(), key=lambda row: row["ts_code"])
    P.atomic_write_json(cache_dir / "stock_basic.json", payload)
    undated = sum(1 for row in payload if not row.get("list_date"))
    return {"rows": len(payload), "without_list_date": undated,
            "from_master_envelope": len(master), "synthesized": len(payload) - len(master)}


def build_stores(
    cache_dir: Path,
    kline: dict[str, Any],
    pools: dict[str, dict[str, Any]],
    master: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Fold raw prices + hfq into the v5 qfq store (``price_store/<ts_code>.json``).

    v5 keeps a single forward-adjusted (qfq) series; the factor / anchor machinery
    is gone, so the qfq four-prices are computed once here (``raw * anchor/factor``,
    anchor = the last available factor so the latest bar's qfq equals its raw --
    matching akshare's qfq anchor convention) and only the result is stored. A
    symbol whose every session lacks an hfq side keeps its qfq basis equal to raw
    (ratios are still valid); a symbol with no hfq side at all is still counted in
    ``symbols_without_factor`` so it is never fabricated.

    ``is_limit_up`` is a K-line-derived field in v5: it is computed locally from
    each symbol's qfq close ratio against the official per-board price-limit
    (主板 10% / 科创·创业板 20% / 北交所 30%) via ``annotate_limit_up`` -- the same
    routine ``validate_cache_contract.py`` recomputes, so the stored value can
    never disagree with the validator. The pool is no longer its source. A session
    without a predecessor (or a missing/non-positive close) gets ``is_limit_up`` =
    None (unknown). Suspended sessions ("未开盘") are dropped and counted, never
    carried forward as a bar. (``pools`` is retained only for the
    ``limit_up_pool`` reference files written by ``build_pools``; it does not feed
    ``is_limit_up`` here.)
    """
    # v5: derive is_limit_up with the same routine the validator uses. Import
    # lazily so unrelated code paths (and environments without akshare) are not
    # forced to pull fetch_akshare_data in at module load.
    try:
        from fetch_akshare_data import annotate_limit_up as _annotate_limit_up
    except Exception:  # noqa: BLE001 - fall back to "unknown" if unavailable
        _annotate_limit_up = None

    written = 0
    no_factor: list[str] = []
    rows_total = 0
    limit_true = limit_false = limit_unknown = 0
    orphan_sessions = 0
    for code in sorted(kline["raw"]):
        sessions = kline["raw"][code]
        hfq = kline["hfq_close"].get(code, {})
        # factor per day = hfq_close / raw_close; anchor = last available factor.
        factors = {
            day: _factor_of(hfq.get(day), price_of(row.get("close")))
            for day, row in sessions.items()
        }
        anchor = None
        for day in sorted(factors):
            if factors[day] is not None:
                anchor = factors[day]
        qfq_rows = []
        symbol_has_factor = False
        for day in sorted(sessions):
            row = dict(sessions[day])
            if price_of(row.get("close")) is None:
                continue  # suspended print -> drop, never store as a bar
            f = factors[day]
            bar = {
                "trade_date": day,
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "is_limit_up": None,  # v5: derived below from the qfq K-line
            }
            if f and anchor:
                scale = anchor / f
                bar["open"] = _scale(row.get("open"), scale)
                bar["high"] = _scale(row.get("high"), scale)
                bar["low"] = _scale(row.get("low"), scale)
                bar["close"] = _scale(row.get("close"), scale)
                symbol_has_factor = True
            else:
                # no hfq side for this day -> basis degenerates to raw for the bar
                bar["open"] = price_of(row.get("open"))
                bar["high"] = price_of(row.get("high"))
                bar["low"] = price_of(row.get("low"))
                bar["close"] = price_of(row.get("close"))
            qfq_rows.append(bar)
        # v5: derive is_limit_up locally (qfq close ratio is scale-invariant, so
        # the result is identical to evaluating the raw basis). Deterministic and
        # matching the validator's recompute.
        if _annotate_limit_up is not None and qfq_rows:
            by_date = {row["trade_date"]: row for row in qfq_rows}
            _annotate_limit_up(by_date, code)
        for row in qfq_rows:
            v = row.get("is_limit_up")
            if v is None:
                limit_unknown += 1
            elif v:
                limit_true += 1
            else:
                limit_false += 1
        if hfq:
            orphan_sessions += len(set(hfq) - set(sessions))
        if not symbol_has_factor:
            no_factor.append(code)
        store = P.build_store(code, (master.get(code) or {}).get("list_date"), qfq_rows,
                              price_mode="neodata_hfq_over_raw")
        P.write_store(cache_dir, store)
        written += 1
        rows_total += len(store["qfq"])
    return {
        "stores": written,
        "qfq_rows": rows_total,
        "symbols_without_factor": len(no_factor),
        "symbols_without_factor_sample": no_factor[:5],
        "hfq_sessions_without_raw": orphan_sessions,
        "is_limit_up_true": limit_true,
        "is_limit_up_false": limit_false,
        "is_limit_up_unknown": limit_unknown,
    }


def build_pools(cache_dir: Path, pools: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Write one file per session, always self-describing about availability."""
    for day, pool in sorted(pools.items()):
        codes = sorted(pool["codes"])
        payload = {
            "schema_version": P.SCHEMA_VERSION,
            "trade_date": day,
            "status": "ok",
            "pool_status": "ok" if codes else "not_collected",
            "availability": "available" if codes else "unavailable",
            "fetched_at": pool["fetched_at"],
            "count": len(codes),
            "codes": codes,
            "source": {
                "tool": pool["source_tool"],
                "trace_id": pool["trace_id"],
                "query": pool["source_query"],
                "proof": "close == limit_up_price for every row",
                "reasoning": pool["reasoning"],
            },
        }
        if not codes:
            payload["availability_reason"] = "upstream_returned_no_rows_for_completed_session"
        P.atomic_write_json(cache_dir / "limit_up_pool" / f"{day}.json", payload)
    return {"sessions": len(pools), "codes_total": sum(len(pool["codes"]) for pool in pools.values())}


def build_manifests(
    cache_dir: Path,
    as_of: str,
    calendar_meta: dict[str, Any],
    master_meta: dict[str, Any],
    store_meta: dict[str, Any],
    pool_meta: dict[str, Any],
    sessions: list[str],
    envelopes: int,
    refusals: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    trace_ids: list[str],
) -> dict[str, Any]:
    """Two manifests: the fetch ledger and the bootstrap summary downstream expects."""
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    coverage = {
        "start": sessions[0] if sessions else None,
        "end": sessions[-1] if sessions else None,
        "session_count": len(sessions),
        "symbol_count": store_meta["stores"],
    }
    ledger = {
        "schema_version": P.SCHEMA_VERSION,
        "mode": "neodata_ingest",
        "generated_at": generated_at,
        "as_of": as_of,
        "source": {
            "kind": "neodata_mcp",
            "transport": "agent_loop",  # MCP is agent-callable only; see module docstring
            "envelopes": envelopes,
            "trace_ids": trace_ids,
        },
        "layers": {
            "raw_landing": "cache/neodata_raw/",
            "authority": [P.STORE_DIR],
            "derived": ["daily_adjusted", "forward_daily", "unadjusted_daily"],
        },
        "calendar": calendar_meta,
        "master": master_meta,
        "price_store": store_meta,
        "limit_up_pool": pool_meta,
        "coverage": coverage,
        "refused_pools": refusals,
        "rejected_envelopes": rejected,
    }
    P.atomic_write_json(cache_dir / "neodata_ingest_manifest.json", ledger)
    bootstrap = {
        "schema_version": P.SCHEMA_VERSION,
        "mode": "neodata_ingest",
        "summary": {
            "schema_version": P.SCHEMA_VERSION,
            "as_of": as_of,
            "source": "neodata_mcp",
            "sessions": sessions,
            "coverage": coverage,
            "universe_size": master_meta["rows"],
            "successful_symbols": store_meta["stores"],
            "failed_symbols": store_meta["symbols_without_factor"],
            "symbol_cache_dir": str(cache_dir / P.STORE_DIR),
            "limit_up_pool_dir": str(cache_dir / "limit_up_pool"),
        },
        "requests": [],
    }
    P.atomic_write_json(cache_dir / "bootstrap_manifest.json", bootstrap)
    return ledger


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------
def run_derive(cache_dir: Path, codes: list[str], python: str) -> dict[str, Any]:
    """Materialize ``daily_adjusted/``, ``forward_daily/``, ``unadjusted_daily/``.

    Shelled out rather than reimplemented: ``derive_cache.py`` is the one place
    that knows how a store projects onto the consumer shapes, and duplicating it
    here is how two derivations start disagreeing.
    """
    script = Path(__file__).resolve().parent / "derive_cache.py"
    command = [python, str(script), "--mode", "derive", "--cache-dir", str(cache_dir), "--qfq-symbols", *codes]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    return {
        "command": " ".join(command[:6] + ["<codes>"]),
        "returncode": completed.returncode,
        "stderr_tail": (completed.stderr or "").strip().splitlines()[-5:],
    }


def ingest(raw_dir: Path, cache_dir: Path, as_of: str, derive: bool = True, python: str = sys.executable) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    envelopes, rejected = load_envelopes(raw_dir)
    by_purpose: dict[str, list[dict[str, Any]]] = {}
    for envelope in envelopes:
        purpose = purpose_of(envelope)
        if not purpose:
            rejected.append({"file": envelope.get("_file"), "reason": "screen_cn_stocks needs an explicit 'purpose'"})
            continue
        by_purpose.setdefault(purpose, []).append(envelope)

    calendar = decode_calendar(by_purpose.get("calendar", []))
    kline = decode_kline(by_purpose.get("kline", []))
    pools = decode_limit_up(by_purpose.get("limit_up", []))
    master = decode_master(by_purpose.get("master", []))

    sessions = sorted({day for rows in kline["raw"].values() for day in rows})
    calendar_meta = build_calendar(cache_dir, calendar, set(sessions), as_of)
    master_meta = build_master(cache_dir, master, kline["names"], kline["markets"])
    store_meta = build_stores(cache_dir, kline, pools["pools"], master)
    pool_meta = build_pools(cache_dir, pools["pools"])

    trace_ids = sorted({str(env.get("trace_id")) for env in envelopes if env.get("trace_id")})
    ledger = build_manifests(
        cache_dir, as_of, calendar_meta, master_meta, store_meta, pool_meta,
        sessions, len(envelopes), pools["refusals"], rejected, trace_ids,
    )
    result = {
        "ok": True,
        "cache_dir": str(cache_dir),
        "raw_dir": str(raw_dir),
        "envelopes": len(envelopes),
        "calendar": calendar_meta,
        "master": master_meta,
        "price_store": store_meta,
        "limit_up_pool": pool_meta,
        "refused_pools": pools["refusals"],
        "rejected_envelopes": rejected,
        "suspend_rows_dropped": kline["suspended_rows"],
        "ignored_qfq_envelopes": kline["ignored_qfq_envelopes"],
        "manifest": "neodata_ingest_manifest.json",
    }
    if derive:
        codes = sorted(kline["raw"])
        result["derive"] = run_derive(cache_dir, codes, python) if codes else {"skipped": "no symbols"}
        if result["derive"].get("returncode") not in (0, None):
            result["ok"] = False
    return result


# --------------------------------------------------------------------------
# self-test: proves the chain without touching the network
# --------------------------------------------------------------------------
def _selftest(tmp: Path) -> int:
    """Build synthetic envelopes, ingest, derive, validate -- assert the contract holds.

    The fixture is deliberately awkward in the ways real data is: one session
    suspended, one symbol whose hfq side is missing, a pool that covers only one
    of the sessions, and a factor that steps mid-window (a bonus issue).
    """
    raw = tmp / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    sessions = ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
    specs = {
        "600519.SH": [1330.0, 1335.0, 1327.5, 1331.0, 1335.0],
        "000001.SZ": [11.10, 11.05, 11.20, 0.0, 11.32],
        "300750.SZ": [230.0, 232.5, 236.1, 240.0, 244.4],
    }
    raw_env, hfq_env = [], []

    def put(name: str, payload: dict[str, Any]) -> None:
        (raw / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for code, closes in specs.items():
        raw_bars, hfq_bars = [], []
        for index, (day, close) in enumerate(zip(sessions, closes)):
            if close == 0.0:
                raw_bars.append({"trade_date": day, "open": "未开盘", "high": "-", "low": "-", "close": "-"})
                hfq_bars.append({"trade_date": day, "open": "未开盘", "high": "-", "low": "-", "close": "-"})
                continue
            raw_bars.append({"trade_date": day, "open": f"{close - 1:.2f}", "high": f"{close + 2:.2f}",
                             "low": f"{close - 3:.2f}", "close": f"{close:.2f}"})
            if code == "300750.SZ":
                continue  # deliberately no hfq side -> factor must stay empty
            factor = 1.0 if index < 3 else 1.5
            hfq_bars.append({"trade_date": day, "open": f"{(close - 1) * factor:.2f}",
                             "high": f"{(close + 2) * factor:.2f}", "low": f"{(close - 3) * factor:.2f}",
                             "close": f"{close * factor:.2f}"})
        raw_env.append({"market_type": "沪市", "code": code, "name": code[:6], "kline_list": raw_bars})
        hfq_env.append({"market_type": "沪市", "code": code, "name": code[:6], "kline_list": hfq_bars})

    put("kline_raw.json", {"tool": "quote_and_kline", "params": {"adjust_type": "0"}, "fetched_at": "2026-10-26T02:00:00+08:00",
                           "trace_id": "selftest-raw", "result": raw_env})
    put("kline_hfq.json", {"tool": "quote_and_kline", "params": {"adjust_type": "2"}, "fetched_at": "2026-10-26T02:00:01+08:00",
                           "trace_id": "selftest-hfq", "result": hfq_env})
    put("calendar.json", {"tool": "trade_calendar", "params": {"code": "SH"}, "fetched_at": "2026-10-26T02:00:02+08:00",
                          "trace_id": "selftest-cal", "result": [
                              {"trading_date": day.replace("-", ""), "market_code": "SH",
                               "market_state": "开市"} for day in sessions
                          ] + [{"trading_date": "20261016", "market_code": "SH", "market_state": "休市（周末休市）"}]})
    pool_text = ("股票代码,股票名称,收盘价(元),涨停价(元)\n"
                 "300750.SZ,宁德时代,244.40,244.40\n"
                 "600519.SH,贵州茅台,1335.00,1335.00\n")
    put("limitup_2026-10-15.json", {
        "tool": "screen_cn_stocks", "purpose": "limit_up",
        "params": {"trade_date": "2026-10-15", "query": "2026年10月15日涨停股票"},
        "fetched_at": "2026-10-26T02:00:03+08:00", "trace_id": "selftest-pool",
        "result": [{"sql": "SELECT ...", "sqlResult": json.dumps({"data": [{"text": pool_text}]}, ensure_ascii=False),
                    "totalCount": 2, "reasoning": "close == limit price"}],
    })
    # a truncated page must be refused, not stored
    put("limitup_truncated.json", {
        "tool": "screen_cn_stocks", "purpose": "limit_up",
        "params": {"trade_date": "2026-10-14", "query": "2026年10月14日涨停股票"},
        "fetched_at": "2026-10-26T02:00:04+08:00", "trace_id": "selftest-pool-truncated",
        "result": [{"sql": "SELECT ...", "sqlResult": json.dumps({"data": [{"text": pool_text}]}, ensure_ascii=False),
                    "totalCount": 84, "reasoning": "page"}],
    })
    # a threshold list must be refused: right codes, wrong proof
    threshold_text = ("股票代码,股票名称,收盘价(元),涨停价(元)\n"
                      "000001.SZ,平安银行,11.32,12.45\n")
    put("limitup_threshold.json", {
        "tool": "screen_cn_stocks", "purpose": "limit_up",
        "params": {"trade_date": "2026-10-13", "query": "涨幅超10%"},
        "fetched_at": "2026-10-26T02:00:05+08:00", "trace_id": "selftest-pool-threshold",
        "result": [{"sql": "SELECT ...", "sqlResult": json.dumps({"data": [{"text": threshold_text}]}, ensure_ascii=False),
                    "totalCount": 1, "reasoning": "pct filter"}],
    })

    cache = tmp / "cache"
    result = ingest(raw, cache, as_of="2026-10-15", derive=True)

    failures: list[str] = []
    if len(result["refused_pools"]) != 2:
        failures.append(f"expected 2 refused pools, got {len(result['refused_pools'])}")
    if result["price_store"]["stores"] != 3:
        failures.append(f"expected 3 stores, got {result['price_store']['stores']}")
    if result["price_store"]["symbols_without_factor"] != 1:
        failures.append("symbol without hfq side should be counted, not fabricated")
    # v5: is_limit_up is K-line-derived, not pool-copied. This quiet fixture has
    # no genuine limit-ups, so every bar (except each symbol's first session,
    # which lacks a predecessor) must be False, and the first session of each of
    # the 3 symbols must be None (unknown).
    if result["price_store"]["is_limit_up_true"] != 0:
        failures.append(f"quiet fixture should yield no limit-ups, got {result['price_store']['is_limit_up_true']}")
    if result["price_store"]["is_limit_up_unknown"] != 3:
        failures.append(f"expected 3 unknown (first session of each symbol), got {result['price_store']['is_limit_up_unknown']}")
    store = P.read_store(cache, "600519.SH")
    if not store:
        failures.append("600519.SH store missing")
    else:
        qfq = store["qfq"]
        if len(qfq) != 5:
            failures.append(f"expected 5 qfq rows, got {len(qfq)}")
        derived = P.derive_rows(store)
        last = derived["qfq"][-1]
        if abs((last["close"] or 0) - 1335.0) > 0.011:
            failures.append(f"qfq anchor wrong: last qfq close {last['close']} != 1335.00")
        if len(derived["raw"]) != 5:
            failures.append(f"600519.SH should carry 5 sessions, got {len(derived['raw'])}")
    # a suspended print must vanish, not become a zero-price bar
    suspended = P.read_store(cache, "000001.SZ")
    if not suspended:
        failures.append("000001.SZ store missing")
    else:
        days = [row["trade_date"] for row in suspended["qfq"]]
        if "2026-10-14" in days:
            failures.append("suspended session 2026-10-14 was stored for 000001.SZ")
        if len(days) != 4:
            failures.append(f"000001.SZ should carry 4 usable sessions, got {len(days)}")
    derive_rc = (result.get("derive") or {}).get("returncode")
    if derive_rc != 0:
        failures.append(f"derive_cache.py failed rc={derive_rc}: {(result.get('derive') or {}).get('stderr_tail')}")
    else:
        for name in ("daily_adjusted", "forward_daily", "unadjusted_daily"):
            if not (cache / name).is_dir():
                failures.append(f"{name}/ not materialized")
        report = validate(cache)
        if not report["ok"]:
            failures.append(f"validator rejected the cache: {report['issues']}")
    print(json.dumps({"selftest": "ok" if not failures else "failed", "failures": failures,
                      "counts": {"stores": result["price_store"]["stores"],
                                 "refused_pools": len(result["refused_pools"]),
                                 "is_limit_up_true": result["price_store"]["is_limit_up_true"],
                                 "is_limit_up_unknown": result["price_store"]["is_limit_up_unknown"]}},
                     ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def validate(cache_dir: Path) -> dict[str, Any]:
    script = Path(__file__).resolve().parents[1] / "validate" / "validate_cache_contract.py"
    completed = subprocess.run([sys.executable, str(script), "--cache-dir", str(cache_dir), "--sample", "10"],
                               capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "issues": [{"level": "error", "code": "validator_unreadable",
                                         "detail": (completed.stderr or "").strip()[-400:]}]}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fold NeoData MCP raw envelopes into the schema v4 cache contract",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See references/neodata_adapter.md for the envelope contract the agent must write.",
    )
    parser.add_argument("--raw-dir", type=Path, help="directory holding one JSON envelope per MCP call")
    parser.add_argument("--cache-dir", type=Path, help="target cache root (schema v4)")
    parser.add_argument("--as-of", default=date.today().isoformat(), help="truncate calendar at this observation date")
    parser.add_argument("--no-derive", action="store_true", help="skip derive_cache.py (store only)")
    parser.add_argument("--validate", action="store_true", help="run validate_cache_contract.py afterwards")
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--selftest", action="store_true", help="run the offline fixture test and exit")
    args = parser.parse_args()

    if args.selftest:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            return _selftest(Path(tmp))

    if not args.raw_dir or not args.cache_dir:
        parser.error("--raw-dir and --cache-dir are required (or pass --selftest)")

    result = ingest(args.raw_dir, args.cache_dir, args.as_of, derive=not args.no_derive)
    if args.validate:
        result["validation"] = validate(args.cache_dir)
        if not result["validation"]["ok"]:
            result["ok"] = False
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
