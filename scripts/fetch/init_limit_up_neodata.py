#!/usr/bin/env python3
"""Initialize **only** the limit-up pool (+ trading calendar) from NeoData MCP output.

Why this exists
---------------
``fetch_akshare_data.py`` sources the limit-up pool from AkShare, whose retention
window measures at ~15 sessions. A 30-session report therefore cannot be backed
by it, and longer windows are impossible. NeoData answers per-date queries back
at least 4 months (verified: 2026-06-10 returned 76 rows), so it can fill the
same contract for ~100 sessions.

Why the work is split in two
----------------------------
MCP tools are reachable only from an agent's conversation loop; no Python
process can call them. So the agent fetches and drops *raw envelopes* on disk,
and this script does every deterministic step (parse, filter, normalize, drop,
write, count). Two runs over the same raw input produce byte-identical caches,
which is the property that lets a report be reproduced next week.

Scope
-----
This script intentionally does **not** touch ``price_store/``, ``daily_adjusted/``
or any price series. The main flow no longer depends on ``limit_up_pool/`` --
``validate_cache_contract`` now checks the derived ``is_limit_up`` field inside
``price_store/<ts_code>.json`` (``check_is_limit_up``), computed on the
forward-adjusted (qfq) basis. This script remains a standalone backfill path
that writes ``limit_up_pool/*.json`` for caches that only need the limit-up set;
``check_daily_coverage`` returns early when ``daily_adjusted/`` is absent, so a
cache holding only ``calendar.json`` + ``limit_up_pool/`` still validates on its
own.

It also imports neither akshare nor ``fetch_akshare_data.py``, so it runs in a
bare interpreter. The contract constants are duplicated on purpose -- importing
the fetcher would drag in akshare, and a shared import is a worse coupling than
two literals that the validator already pins.

Usage
-----
    # 1) after the agent has dropped the trade_calendar envelope:
    python init_limit_up_neodata.py plan --cache-dir CACHE --raw-dir CACHE/neodata_raw

    # 2) the agent executes plan.jsonl (one screen_cn_stocks call per line),
    #    dropping one envelope per session under --raw-dir.

    # 3) convert everything into the v4 contract:
    python init_limit_up_neodata.py ingest --cache-dir CACHE --raw-dir CACHE/neodata_raw

    # 4) progress at any time (safe to run mid-backfill):
    python init_limit_up_neodata.py status --cache-dir CACHE

    # offline logic check, no network, no files touched:
    python init_limit_up_neodata.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 4
POOL_STATUSES = ("ok", "not_collected", "error")
DEFAULT_SESSIONS = 100
MAX_ROWS_PER_CALL = 100  # hard upstream cap on screen_cn_stocks
# Two decimals are the A-share tick; this tolerance only absorbs the upstream's
# own rounding of limit_up_price vs close. It is NOT a "close enough to be a
# limit-up" threshold -- a non-limit-up close differs by far more than this.
LIMIT_TOLERANCE = 0.0055
CODE_RE = re.compile(r"(\d{6})")
ROWS_IN_SET_RE = re.compile(r"\b\d+\s+rows?\s+in\s+set\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Board map. Anything outside it (BSE 43/83/87/88/920, B-share 900/200) is
# dropped: the skill covers the four A-share boards only, and a 30%-band BSE
# limit-up would silently inflate every breadth figure.
BOARD_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("600", "601", "603", "605"), "sh_main"),
    (("000", "001", "002", "003"), "sz_main"),
    (("300", "301"), "chinext"),
    (("688", "689"), "star"),
)

# Chinese label per board, used to build the split queries when a session
# exceeds the 100-row cap. Measured: 2026-06-01 had 168 limit-ups, i.e. the cap
# is hit on ordinary active days, not only in extreme ones.
BOARD_CN: dict[str, str] = {
    "sh_main": "沪市主板",
    "sz_main": "深市主板",
    "chinext": "创业板",
    "star": "科创板",
}
BOARD_ORDER: tuple[str, ...] = ("sh_main", "sz_main", "chinext", "star")

QUERY_TEMPLATE = (
    "{cn_date}全部涨停股票的代码、简称、收盘价、涨停价、是否ST"
)
# The board enumeration is also enforced in code; it is in the prompt only so a
# compliant model returns the right shape the first time.
PLAN_QUERY_TEMPLATE = (
    "沪深主板、创业板、科创板中，{cn_date}全部涨停股票的代码、简称、收盘价、涨停价、是否ST"
)
# Used only when a session overflows the 100-row cap and must be split by board.
PART_QUERY_TEMPLATE = (
    "{cn_date}{board_cn}涨停股票的代码、简称、收盘价、涨停价、是否ST"
)
SPLIT_REASONS = ("truncated:", "board_split_incomplete:", "missing_limit_price_column")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_date(value: Any) -> str | None:
    """Return ``YYYY-MM-DD`` from the many shapes upstream uses, else None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ISO_DATE_RE.match(text):
        return text
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return None


def cn_date(iso: str) -> str:
    """``2026-06-10`` -> ``2026年6月10日`` (the phrasing NeoData parses best)."""
    year, month, day = iso.split("-")
    return f"{int(year)}年{int(month)}月{int(day)}日"


def symbol6(raw: Any) -> str | None:
    match = CODE_RE.search(str(raw or ""))
    return match.group(1) if match else None


def board_of(code: str) -> str | None:
    for prefixes, name in BOARD_PREFIXES:
        if code.startswith(prefixes):
            return name
    return None


def is_bse(raw: Any, code: str | None) -> bool:
    if ".BJ" in str(raw or "").upper():
        return True
    return bool(code) and code.startswith(("43", "83", "87", "88", "920"))


def normalize_name(raw: Any) -> str:
    """NFKC (full-width -> ASCII), drop every space, strip XD/XR/DR prefixes.

    Measured upstream noise: ``特  力Ａ`` (double space + full-width A),
    ``红 宝 丽``, ``XD恒兴新``, ``XDST嘉澳``. Names are diagnostics only -- the
    contract stores codes -- but a ST test run on ``XDST嘉澳`` must still work.
    """
    text = unicodedata.normalize("NFKC", str(raw or ""))
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"^(XD|XR|DR)(?=.)", "", text, flags=re.IGNORECASE)
    return text.strip()


def looks_st(name: str) -> bool:
    return "ST" in name.upper()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# envelope parsing
# --------------------------------------------------------------------------- #
def parse_csv_blob(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse the ``A,B,C\\n1,2,3\\n\\n76 rows in set`` blob NeoData returns.

    The CSV header is model-chosen, so callers must resolve columns by keyword
    rather than by position.
    """
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return [], []
    header = [cell.strip() for cell in lines[0].split(",")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if ROWS_IN_SET_RE.search(line):
            break
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        rows.append(dict(zip(header, cells[: len(header)])))
    return header, rows


def envelope_rows(envelope: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None, list[str]]:
    """Extract rows + the upstream total from either envelope shape.

    ``screen_cn_stocks`` returns ``result.sqlResult`` as a *string of JSON* whose
    ``data[].text`` is CSV. ``trade_calendar`` returns a structured list. Both
    shapes arrive here.
    """
    result = envelope.get("result")
    reasons: list[str] = []

    if isinstance(result, list):
        if result and all(isinstance(item, str) for item in result):
            # trimmed calendar: a plain list of dates, state implied open
            return list(result), None, reasons  # type: ignore[return-value]
        rows = [row for row in result if isinstance(row, dict)]
        total = envelope.get("total_count")
        if total is None:
            total = sum(int(row.get("totalCount") or 0) for row in rows if "totalCount" in row) or None
        return rows, total, reasons

    if not isinstance(result, dict):
        return [], None, ["result_missing"]

    total = result.get("totalCount")
    if total is None:
        total = result.get("total_count")

    blob = result.get("sqlResult")
    if isinstance(blob, str) and blob.strip():
        try:
            inner = json.loads(blob)
        except json.JSONDecodeError:
            reasons.append("sqlResult_not_json")
            return [], total, reasons
        if isinstance(inner, dict):
            if inner.get("totalCount") is not None:
                total = inner.get("totalCount")
            payload = inner.get("data")
            rows: list[dict[str, Any]] = []
            for item in payload if isinstance(payload, list) else []:
                if isinstance(item, dict):
                    if "text" in item:
                        _, parsed = parse_csv_blob(item.get("text") or "")
                        rows.extend(parsed)
                    else:
                        rows.append(item)
                elif isinstance(item, str):
                    _, parsed = parse_csv_blob(item)
                    rows.extend(parsed)
            if not rows:
                reasons.append("sqlResult_no_rows")
            return rows, total, reasons
        if isinstance(inner, list):
            return [row for row in inner if isinstance(row, dict)], total, reasons

    # structured payload under result.data
    payload = result.get("data")
    if isinstance(payload, list):
        rows = []
        for item in payload:
            if isinstance(item, dict) and "text" in item:
                _, parsed = parse_csv_blob(item.get("text") or "")
                rows.extend(parsed)
            elif isinstance(item, dict):
                rows.append(item)
        return rows, total, reasons

    if result and all(not isinstance(value, (list, dict)) for value in result.values()):
        return [dict(result)], total, reasons
    return [], total, reasons


def pick_column(header: Iterable[str], keywords: tuple[str, ...]) -> str | None:
    for column in header:
        lowered = unicodedata.normalize("NFKC", column).upper()
        for keyword in keywords:
            if keyword.upper() in lowered:
                return column
    return None


def to_float(value: Any) -> float | None:
    try:
        text = str(value).replace(",", "").strip()
        return float(text) if text not in ("", "-", "--", "None", "null") else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# pool construction
# --------------------------------------------------------------------------- #
def build_pool(
    *,
    trade_date: str,
    rows: list[dict[str, Any]],
    upstream_total: int | None,
    envelope_meta: dict[str, Any],
    fetched_at: str,
) -> dict[str, Any]:
    """Turn one session's rows into a v4 ``limit_up_pool`` payload.

    Every rejection path here exists because the alternative is a silent lie:
    a truncated page read as a full pool, or a pct_change threshold read as a
    limit-up. Both are explicitly forbidden by the skill contract.
    """
    diagnostics: dict[str, Any] = {
        "rows_returned": len(rows),
        "upstream_total": upstream_total,
        "dropped": {"bse_or_other_board": 0, "bad_code": 0, "not_at_limit": 0},
        "boards": {},
        "st": 0,
    }
    base = {
        "schema_version": SCHEMA_VERSION,
        "trade_date": trade_date,
        "fetched_at": fetched_at,
        "source": {
            "provider": "neodata-mcp",
            "tool": "screen_cn_stocks",
            "trace_id": envelope_meta.get("trace_id"),
            "cost": envelope_meta.get("cost"),
            "upstream_total": upstream_total,
            "rows_returned": len(rows),
        },
        "diagnostics": diagnostics,
    }

    def failure(pool_status: str, status: str, reason: str, availability: str) -> dict[str, Any]:
        payload = dict(base)
        payload.update(
            {
                "status": status,
                "pool_status": pool_status,
                "availability": availability,
                "availability_reason": reason,
                "count": 0,
                "codes": [],
            }
        )
        return payload

    if not rows:
        # "upstream kept nothing" is unknown, never "zero limit-ups": a completed
        # session always has at least one. Mirrors fetch_akshare_data semantics.
        return failure("not_collected", "ok", "upstream_returned_no_rows", "unavailable")

    header = list(rows[0].keys())
    col_code = pick_column(header, ("代码", "code"))
    col_name = pick_column(header, ("名称", "简称", "name"))
    col_close = pick_column(header, ("收盘",))
    col_limit = pick_column(header, ("涨停价",))

    if col_code is None:
        return failure("error", "error", "missing_code_column", "error")
    if col_close is None or col_limit is None:
        # Hard red line: without an explicit limit_up_price there is no legal way
        # to decide membership, so refuse rather than fall back to pct_change.
        return failure("error", "error", "missing_limit_price_column", "error")

    if upstream_total is not None and upstream_total > len(rows):
        # A page truncated at limit=100 must never be stored as the session's pool.
        return failure(
            "error",
            "error",
            f"truncated:{len(rows)}/{upstream_total}",
            "error",
        )

    codes: set[str] = set()
    for row in rows:
        raw_code = row.get(col_code)
        code = symbol6(raw_code)
        if code is None:
            diagnostics["dropped"]["bad_code"] += 1
            continue
        if is_bse(raw_code, code):
            diagnostics["dropped"]["bse_or_other_board"] += 1
            continue
        board = board_of(code)
        if board is None:
            diagnostics["dropped"]["bse_or_other_board"] += 1
            continue
        close = to_float(row.get(col_close))
        limit = to_float(row.get(col_limit))
        if close is None or limit is None or abs(close - limit) > LIMIT_TOLERANCE:
            diagnostics["dropped"]["not_at_limit"] += 1
            continue
        codes.add(code)
        diagnostics["boards"][board] = diagnostics["boards"].get(board, 0) + 1
        if col_name and looks_st(normalize_name(row.get(col_name))):
            diagnostics["st"] += 1

    if not codes:
        return failure("error", "error", "all_rows_filtered_out", "error")

    payload = dict(base)
    payload.update(
        {
            "status": "ok",
            "pool_status": "ok",
            "availability": "available",
            "count": len(codes),
            "codes": sorted(codes),
        }
    )
    return payload


def build_pool_from_parts(
    *,
    trade_date: str,
    parts: dict[str, tuple[list[dict[str, Any]], int | None, dict[str, Any]]],
    fetched_at: str,
) -> dict[str, Any]:
    """Merge per-board pools for a session that overflows the 100-row cap.

    A session with 168 limit-ups cannot be read in one call, and a truncated page
    must never be stored. Splitting by board is safe because a single board
    rarely exceeds 100 in a day, and every part carries its own ``totalCount``,
    so a part that is *itself* truncated is still caught here.

    The missing-piece rule matters as much as the truncation rule: a board that
    was never queried is unknown, not zero, so a partial board set is an error
    rather than a smaller pool.
    """
    sub_pools: dict[str, dict[str, Any]] = {}
    dropped = {"bse_or_other_board": 0, "bad_code": 0, "not_at_limit": 0}
    boards: dict[str, int] = {}
    st_total = 0
    trace_ids: list[str] = []
    failures: list[str] = []

    for board in BOARD_ORDER:
        entry = parts.get(board)
        if entry is None:
            failures.append(f"{board}:not_fetched")
            continue
        rows, upstream_total, meta = entry
        sub = build_pool(
            trade_date=trade_date,
            rows=rows,
            upstream_total=upstream_total,
            envelope_meta=meta,
            fetched_at=fetched_at,
        )
        sub_pools[board] = {
            "status": sub["pool_status"],
            "reason": sub.get("availability_reason"),
            "upstream_total": upstream_total,
            "rows_returned": len(rows),
            "codes": sub["count"],
        }
        if meta.get("trace_id"):
            trace_ids.append(str(meta["trace_id"]))
        if sub["pool_status"] == "error":
            failures.append(f"{board}:{sub.get('availability_reason')}")
            continue
        # "not_collected" for a single board legitimately means that board had no
        # limit-up that day; it is not a gap once every board has been queried.
        for key, value in sub["diagnostics"]["dropped"].items():
            dropped[key] = dropped.get(key, 0) + value
        for name, count in (sub["diagnostics"]["boards"] or {}).items():
            boards[name] = boards.get(name, 0) + count
        st_total += int(sub["diagnostics"]["st"] or 0)

    extra_boards = sorted(set(parts) - set(BOARD_ORDER))

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "trade_date": trade_date,
        "fetched_at": fetched_at,
        "source": {
            "provider": "neodata-mcp",
            "tool": "screen_cn_stocks",
            "mode": "board_split",
            "trace_ids": trace_ids,
            "parts": sub_pools,
        },
        "diagnostics": {
            "mode": "board_split",
            "parts": sub_pools,
            "dropped": dropped,
            "boards": boards,
            "st": st_total,
            "unexpected_boards": extra_boards,
        },
    }

    if failures:
        payload.update(
            {
                "status": "error",
                "pool_status": "error",
                "availability": "error",
                "availability_reason": "board_split_incomplete:" + ",".join(failures),
                "count": 0,
                "codes": [],
            }
        )
        return payload

    codes: set[str] = set()
    for board in BOARD_ORDER:
        rows, _, _ = parts[board]
        sub = build_pool(
            trade_date=trade_date,
            rows=rows,
            upstream_total=parts[board][1],
            envelope_meta=parts[board][2],
            fetched_at=fetched_at,
        )
        codes.update(sub["codes"])

    if not codes:
        payload.update(
            {
                "status": "error",
                "pool_status": "error",
                "availability": "error",
                "availability_reason": "all_rows_filtered_out",
                "count": 0,
                "codes": [],
            }
        )
        return payload

    payload.update(
        {
            "status": "ok",
            "pool_status": "ok",
            "availability": "available",
            "count": len(codes),
            "codes": sorted(codes),
        }
    )
    return payload


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #
def calendar_dates_from_envelope(envelope: dict[str, Any]) -> list[str]:
    rows, _, _ = envelope_rows(envelope)
    dates: set[str] = set()
    for row in rows:
        # A bare "YYYY-MM-DD" string is also accepted: when the agent trims the
        # calendar to open sessions only, the state is implied and carrying a
        # per-row dict would be pure overhead.
        if isinstance(row, str):
            day = normalize_date(row)
            if day:
                dates.add(day)
            continue
        # NeoData uses ``trading_date`` (20260925); older/other shapes use
        # cal_date / trade_date. All are normalized to YYYY-MM-DD.
        day = normalize_date(
            row.get("trading_date")
            or row.get("cal_date")
            or row.get("trade_date")
            or row.get("date")
            or row.get("日期")
        )
        if day is None:
            continue
        state = str(
            row.get("market_state") or row.get("状态") or row.get("is_open") or ""
        )
        if "休市" in state:
            continue
        if state.strip() in ("0", "False", "false"):
            continue
        dates.add(day)
    return sorted(dates)


def load_calendar(cache_dir: Path) -> list[str]:
    payload = read_json(cache_dir / "calendar.json")
    if not isinstance(payload, list):
        return []
    dates = []
    for row in payload:
        if isinstance(row, dict):
            day = normalize_date(row.get("cal_date") or row.get("trade_date"))
            if day and row.get("is_open", 1) in (1, "1", True, "true", "True"):
                dates.append(day)
    return sorted(set(dates))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_plan(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir).resolve()
    raw_dir = Path(args.raw_dir).resolve() if args.raw_dir else cache_dir / "neodata_raw"

    dates = load_calendar(cache_dir)
    if not dates:
        calendar_envelopes = [
            path for path in sorted(raw_dir.glob("*.json"))
            if (read_json(path) or {}).get("tool") == "trade_calendar"
        ] if raw_dir.is_dir() else []
        if not calendar_envelopes:
            print(
                f"no calendar: {cache_dir / 'calendar.json'} is missing and no "
                f"trade_calendar envelope was found under {raw_dir}",
                file=sys.stderr,
            )
            return 2
        merged: set[str] = set()
        for path in calendar_envelopes:
            merged.update(calendar_dates_from_envelope(read_json(path) or {}))
        dates = sorted(merged)

    as_of = normalize_date(args.as_of) or (dates[-1] if dates else None)
    if as_of is None:
        print("cannot determine --as-of: no calendar dates available", file=sys.stderr)
        return 2

    sessions = [day for day in dates if day <= as_of][-args.sessions:]
    if not sessions:
        print(f"calendar has no sessions on or before {as_of}", file=sys.stderr)
        return 2
    eligible = [day for day in dates if day <= as_of]
    if len(eligible) < args.sessions:
        # trade_calendar caps at 100 rows per call, so a 100-session window needs
        # roughly 150 calendar days and therefore two envelopes.
        print(
            f"warning: calendar only covers {len(eligible)} sessions on or before {as_of}, "
            f"but {args.sessions} were requested; widen the trade_calendar range",
            file=sys.stderr,
        )

    existing = {
        path.stem
        for path in (cache_dir / "limit_up_pool").glob("*.json")
    } if (cache_dir / "limit_up_pool").is_dir() else set()
    done: set[str] = set()
    needs_split: set[str] = set()
    for stem in existing:
        payload = read_json(cache_dir / "limit_up_pool" / f"{stem}.json") or {}
        if payload.get("status") == "ok":
            done.add(stem)
            continue
        reason = str(payload.get("availability_reason") or "")
        if any(reason.startswith(prefix) for prefix in SPLIT_REASONS):
            needs_split.add(stem)

    out_path = Path(args.out).resolve() if args.out else raw_dir / "plan.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tasks: list[dict[str, Any]] = []
    for day in sessions:
        if day in done:
            continue
        if day in needs_split:
            # A session that already failed on the row cap must be re-fetched per
            # board; repeating the full query would just truncate again.
            for board in BOARD_ORDER:
                tasks.append(
                    {
                        "trade_date": day,
                        "tool": "screen_cn_stocks",
                        "purpose": "limit_up_part",
                        "board": board,
                        "board_cn": BOARD_CN[board],
                        "limit": MAX_ROWS_PER_CALL,
                        "query": PART_QUERY_TEMPLATE.format(
                            cn_date=cn_date(day), board_cn=BOARD_CN[board]
                        ),
                        "target": f"limit_up_pool/{day}.json",
                    }
                )
            continue
        tasks.append(
            {
                "trade_date": day,
                "tool": "screen_cn_stocks",
                "purpose": "limit_up",
                "limit": MAX_ROWS_PER_CALL,
                "query": PLAN_QUERY_TEMPLATE.format(cn_date=cn_date(day)),
                "target": f"limit_up_pool/{day}.json",
            }
        )

    with out_path.open("w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False) + "\n")

    fresh = [task["trade_date"] for task in tasks if task["purpose"] == "limit_up"]
    print(f"as_of           : {as_of}")
    print(f"sessions        : {len(sessions)}  {sessions[0]} .. {sessions[-1]}")
    print(f"already ok      : {len([d for d in sessions if d in done])}")
    print(f"needs split     : {len([d for d in sessions if d in needs_split])}")
    print(f"pending sessions: {len(set(task['trade_date'] for task in tasks))}")
    print(f"pending calls   : {len(tasks)}  ({len(fresh)} full + {len(tasks) - len(fresh)} board parts)")
    print(f"plan written    : {out_path}")
    if tasks:
        print(f"first call      : {tasks[0]['query']}")
    else:
        print("nothing pending; every session in the window already validated")
    return 0


def iterate_envelopes(raw_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    envelopes: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    if not raw_dir.is_dir():
        return envelopes, rejected
    for path in sorted(raw_dir.glob("*.json")):
        payload = read_json(path)
        if not isinstance(payload, dict) or not payload.get("tool"):
            if path.name in ("calendar.json", "calendar_upstream.json"):
                continue  # not envelopes; cache outputs that may live here
            rejected.append({"file": path.name, "reason": "not_an_envelope"})
            continue
        payload["_file"] = path.name
        envelopes.append(payload)
    return envelopes, rejected


def resolve_trade_date(envelope: dict[str, Any]) -> str | None:
    params = envelope.get("params") or {}
    for key in ("trade_date", "date", "start_date"):
        day = normalize_date(params.get(key))
        if day:
            return day
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(envelope.get("_file") or ""))
    if match:
        return match.group(0)
    return None


def resolve_board(envelope: dict[str, Any]) -> str:
    params = envelope.get("params") or {}
    return str(params.get("board") or envelope.get("board") or "").strip()


def cmd_ingest(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir).resolve()
    raw_dir = Path(args.raw_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    envelopes, rejected = iterate_envelopes(raw_dir)
    if not envelopes and not rejected:
        print(f"no envelopes under {raw_dir}", file=sys.stderr)
        return 2

    calendar_envelopes = [env for env in envelopes if env.get("tool") == "trade_calendar"]
    pool_envelopes = [
        env for env in envelopes
        if env.get("tool") == "screen_cn_stocks"
        and env.get("purpose", "limit_up") in ("limit_up", "limit_up_part")
    ]

    # ---- calendar -------------------------------------------------------- #
    calendar_note = "kept existing"
    if calendar_envelopes:
        merged: set[str] = set()
        fetched_stamps: list[str] = []
        for env in calendar_envelopes:
            merged.update(calendar_dates_from_envelope(env))
            stamp = env.get("fetched_at")
            if stamp:
                fetched_stamps.append(str(stamp))
        as_of = normalize_date(args.as_of) or (max(merged) if merged else None)
        if merged and as_of:
            upstream = [
                {"cal_date": day, "is_open": 1} for day in sorted(merged)
            ]
            atomic_write_json(cache_dir / "calendar_upstream.json", upstream)
            atomic_write_json(
                cache_dir / "calendar.json",
                [row for row in upstream if row["cal_date"] <= as_of],
            )
            calendar_note = f"{len(upstream)} sessions, truncated at {as_of}"

    dates = load_calendar(cache_dir)
    as_of = normalize_date(args.as_of) or (dates[-1] if dates else None)
    if as_of is None:
        print("cannot determine --as-of and no calendar present", file=sys.stderr)
        return 2
    window = [day for day in dates if day <= as_of][-args.sessions:]
    window_set = set(window)

    # ---- pools ----------------------------------------------------------- #
    written: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []
    full_pools: dict[str, dict[str, Any]] = {}
    split_parts: dict[str, dict[str, tuple[list[dict[str, Any]], int | None, dict[str, Any]]]] = {}
    stamps: dict[str, str] = {}

    for env in pool_envelopes:
        trade_date = resolve_trade_date(env)
        if trade_date is None:
            rejected.append({"file": env.get("_file", "?"), "reason": "no_trade_date"})
            continue
        if dates and trade_date not in window_set:
            skipped.append(
                {"file": env.get("_file", "?"), "trade_date": trade_date,
                 "reason": "outside_requested_window"}
            )
            continue
        if dates and trade_date not in dates:
            skipped.append(
                {"file": env.get("_file", "?"), "trade_date": trade_date,
                 "reason": "not_a_trading_session"}
            )
            continue

        rows, total, parse_notes = envelope_rows(env)
        metadata = env.get("metadata") or {}
        # Prefer the envelope's own stamp so re-ingesting is reproducible; fall
        # back to the file mtime, which also does not change between runs.
        fetched_at = str(env.get("fetched_at") or "")
        if not fetched_at:
            try:
                fetched_at = datetime.fromtimestamp(
                    (raw_dir / env["_file"]).stat().st_mtime
                ).astimezone().isoformat(timespec="seconds")
            except (OSError, KeyError):
                fetched_at = now_iso()
        if parse_notes:
            rejected.append(
                {"file": env.get("_file", "?"), "reason": ",".join(parse_notes),
                 "trade_date": trade_date}
            )
        stamps.setdefault(trade_date, fetched_at)

        meta = {"trace_id": metadata.get("trace_id"), "cost": metadata.get("cost")}
        if env.get("purpose") == "limit_up_part":
            board = resolve_board(env)
            if board not in BOARD_ORDER:
                rejected.append(
                    {"file": env.get("_file", "?"), "trade_date": trade_date,
                     "reason": f"unknown_board:{board or '-'}"}
                )
                continue
            split_parts.setdefault(trade_date, {})[board] = (rows, total, meta)
        else:
            # A complete board split supersedes a full-session envelope, because
            # the latter is exactly the one that hit the row cap.
            full_pools.setdefault(
                trade_date, {"rows": rows, "total": total, "meta": meta}
            )

    for trade_date in sorted(set(full_pools) | set(split_parts)):
        fetched_at = stamps.get(trade_date) or now_iso()
        if trade_date in split_parts:
            payload = build_pool_from_parts(
                trade_date=trade_date,
                parts=split_parts[trade_date],
                fetched_at=fetched_at,
            )
        else:
            entry = full_pools[trade_date]
            payload = build_pool(
                trade_date=trade_date,
                rows=entry["rows"],
                upstream_total=entry["total"],
                envelope_meta=entry["meta"],
                fetched_at=fetched_at,
            )
        atomic_write_json(cache_dir / "limit_up_pool" / f"{trade_date}.json", payload)
        written[trade_date] = payload

    # ---- manifest -------------------------------------------------------- #
    pool_dir = cache_dir / "limit_up_pool"
    all_pools: dict[str, dict[str, Any]] = {}
    for path in sorted(pool_dir.glob("*.json")):
        payload = read_json(path)
        if isinstance(payload, dict):
            all_pools[path.stem] = payload

    in_window = {day: all_pools[day] for day in window if day in all_pools}
    counts = {"ok": 0, "not_collected": 0, "error": 0}
    total_codes = 0
    rows_summary: list[dict[str, Any]] = []
    for day in sorted(in_window):
        payload = in_window[day]
        status = str(payload.get("pool_status") or "error")
        counts[status] = counts.get(status, 0) + 1
        total_codes += int(payload.get("count") or 0)
        rows_summary.append(
            {
                "trade_date": day,
                "status": payload.get("status"),
                "pool_status": status,
                "count": payload.get("count"),
                "reason": payload.get("availability_reason"),
                "trace_id": (payload.get("source") or {}).get("trace_id"),
            }
        )

    missing = [day for day in window if day not in in_window]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "provider": "neodata-mcp",
        "scope": "limit_up_pool+calendar",
        "as_of": as_of,
        "sessions_requested": args.sessions,
        "window": {"start": window[0] if window else None,
                   "end": window[-1] if window else None,
                   "count": len(window)},
        "calendar": calendar_note,
        "summary": {
            "pool_files": len(in_window),
            "missing_sessions": len(missing),
            "ok": counts["ok"],
            "not_collected": counts["not_collected"],
            "error": counts["error"],
            "total_limit_up_codes": total_codes,
        },
        "missing": missing,
        "pools": rows_summary,
        "rejected_files": rejected,
        "skipped_files": skipped,
    }
    atomic_write_json(cache_dir / "neodata_limit_up_manifest.json", manifest)

    # ---- report ---------------------------------------------------------- #
    print(f"cache dir       : {cache_dir}")
    print(f"as_of / window  : {as_of}  {window[0] if window else '-'} .. {window[-1] if window else '-'}  ({len(window)} sessions)")
    print(f"calendar        : {calendar_note}")
    print(f"pools written   : {len(written)} this run")
    print(f"ok              : {counts['ok']}")
    print(f"not_collected   : {counts['not_collected']}")
    print(f"error           : {counts['error']}")
    print(f"missing         : {len(missing)}")
    print(f"codes total     : {total_codes}")
    if rejected:
        print(f"rejected        : {len(rejected)}")
        for item in rejected[:5]:
            print(f"  - {item.get('file')}: {item.get('reason')}")
    if skipped:
        print(f"skipped         : {len(skipped)}")
        for item in skipped[:5]:
            print(f"  - {item.get('file')}: {item.get('reason')}")

    # Be explicit about what this scope does and does not satisfy. The whole
    # validator still wants stock_basic.json (master) and a price layer, so a
    # limit-up-only directory is a clean *partial* cache, not an ok:true one.
    print(
        "note            : this scope writes calendar + limit_up_pool only. "
        "validate_cache_contract still needs stock_basic.json + a price layer "
        "for ok:true; its limit_up_pool checks pass on their own."
    )

    if args.strict and (missing or counts["error"] or counts["not_collected"]):
        print("strict: window is not fully satisfied", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir).resolve()
    dates = load_calendar(cache_dir)
    pool_dir = cache_dir / "limit_up_pool"
    manifest = read_json(cache_dir / "neodata_limit_up_manifest.json") or {}

    as_of = normalize_date(args.as_of) or (dates[-1] if dates else None)
    window = [day for day in dates if day <= as_of][-args.sessions:] if as_of else []

    present = {path.stem for path in pool_dir.glob("*.json")} if pool_dir.is_dir() else set()
    buckets: dict[str, list[str]] = {"ok": [], "not_collected": [], "error": [], "absent": []}
    for day in window:
        if day not in present:
            buckets["absent"].append(day)
            continue
        payload = read_json(pool_dir / f"{day}.json") or {}
        status = str(payload.get("pool_status") or "error")
        buckets.setdefault(status, []).append(day)

    print(f"cache dir   : {cache_dir}")
    print(f"calendar    : {len(dates)} sessions" + (f", last {dates[-1]}" if dates else ""))
    print(f"window      : {len(window)} sessions"
          + (f"  {window[0]} .. {window[-1]}" if window else ""))
    for name in ("ok", "not_collected", "error", "absent"):
        days = buckets.get(name) or []
        suffix = ""
        if days and name in ("error", "not_collected", "absent"):
            suffix = f"  e.g. {', '.join(days[:3])}"
        print(f"  {name:<14}: {len(days)}{suffix}")
    if isinstance(manifest.get("summary"), dict):
        print(f"manifest    : {json.dumps(manifest['summary'], ensure_ascii=False)}")
    if not dates:
        print("hint        : no calendar.json -- run plan/ingest with a trade_calendar envelope first")
    return 0


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def _csv_envelope(rows_csv: str, total: int, trace_id: str = "self") -> dict[str, Any]:
    inner = json.dumps(
        {"data": [{"text": rows_csv}], "totalCount": total, "status": "success"},
        ensure_ascii=False,
    )
    return {
        "tool": "screen_cn_stocks",
        "purpose": "limit_up",
        "params": {"trade_date": "2026-10-09"},
        "fetched_at": "2026-10-26T02:20:00+08:00",
        "metadata": {"trace_id": trace_id, "cost": "2900"},
        "result": {"sql": "", "sqlResult": inner},
    }


def selftest() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # --- CSV blob + envelope unwrapping ---------------------------------- #
    blob = (
        "股票代码,股票名称,收盘价(元),涨停价(元),是否ST\n"
        "300530.SZ,领湃科技,28.9000,28.9000,否\n"
        "920271.BJ,邦德股份,14.7900,14.7900,否\n"
        "603822.SH,XDST嘉澳,83.2100,83.2100,是\n"
        "600302.SH,ST标准,10.8900,10.8900,是\n"
        "900901.SH,云赛Ｂ股,1.0000,1.0000,否\n"
        "002409.SZ,雅克科技,122.5500,120.0000,否\n"
        "\n6 rows in set\n"
    )
    envelope = _csv_envelope(blob, 6)
    rows, total, notes = envelope_rows(envelope)
    check(len(rows) == 6, f"csv rows: expected 6, got {len(rows)}")
    check(total == 6, f"totalCount: expected 6, got {total}")
    check(not notes, f"unexpected parse notes: {notes}")
    check(rows[0]["股票名称"] == "领湃科技", "csv header/dict mapping is off")

    payload = build_pool(
        trade_date="2026-10-09",
        rows=rows,
        upstream_total=total,
        envelope_meta={"trace_id": "self", "cost": "2900"},
        fetched_at="2026-10-26T02:20:00+08:00",
    )
    check(payload["status"] == "ok", f"happy path status: {payload['status']}")
    check(payload["pool_status"] == "ok", f"happy path pool_status: {payload['pool_status']}")
    check(payload["count"] == len(payload["codes"]), "count must equal len(codes)")
    # 300530 (CHINEXT), 603822 + 600302 (SH main) are at the limit; 002409 closed
    # below it; 920271.BJ and 900901 (B share) are outside the four boards.
    check(payload["codes"] == ["300530", "600302", "603822"],
          f"unexpected codes: {payload['codes']}")
    dropped = payload["diagnostics"]["dropped"]
    check(dropped["bse_or_other_board"] == 2, f"BSE/B-share drop: {dropped}")
    check(dropped["not_at_limit"] == 1, f"non-limit drop: {dropped}")
    check(payload["diagnostics"]["st"] == 2, f"ST count: {payload['diagnostics']}")
    check(payload["diagnostics"]["boards"] == {"chinext": 1, "sh_main": 2},
          f"boards: {payload['diagnostics']['boards']}")
    check(payload["schema_version"] == 4, "schema_version must be 4")

    # --- truncation must never be stored as a full pool ------------------- #
    envelope = _csv_envelope(blob, 150)
    rows, total, _ = envelope_rows(envelope)
    payload = build_pool(
        trade_date="2026-10-09", rows=rows, upstream_total=total,
        envelope_meta={"trace_id": "self"}, fetched_at="2026-10-26T02:20:00+08:00",
    )
    check(payload["status"] == "error", "truncated page must be status=error")
    check(payload["pool_status"] == "error", "truncated page must be pool_status=error")
    check(payload["codes"] == [], "truncated page must not carry partial codes")
    check("truncated" in str(payload.get("availability_reason")), "truncation reason missing")

    # --- missing limit_up_price is a hard refusal ------------------------- #
    no_limit = "股票代码,股票名称,收盘价(元),涨跌幅(%)\n300530.SZ,领湃科技,28.9,20.02\n"
    envelope = _csv_envelope(no_limit, 1)
    rows, total, _ = envelope_rows(envelope)
    payload = build_pool(
        trade_date="2026-10-09", rows=rows, upstream_total=total,
        envelope_meta={}, fetched_at="2026-10-26T02:20:00+08:00",
    )
    check(payload["pool_status"] == "error", "missing limit price must be refused")
    check(payload.get("availability_reason") == "missing_limit_price_column",
          f"missing-limit reason: {payload.get('availability_reason')}")

    # --- empty upstream is unknown, not zero ------------------------------ #
    envelope = _csv_envelope("股票代码,股票名称,收盘价(元),涨停价(元),是否ST\n\n0 rows in set\n", 0)
    rows, total, _ = envelope_rows(envelope)
    payload = build_pool(
        trade_date="2026-10-09", rows=rows, upstream_total=total,
        envelope_meta={}, fetched_at="2026-10-26T02:20:00+08:00",
    )
    check(payload["pool_status"] == "not_collected", "empty must be not_collected")
    check(payload["availability"] == "unavailable", "empty availability must be unavailable")
    check(payload["codes"] == [], "empty pool codes must be []")

    # --- name normalization (diagnostics only, but ST depends on it) ------ #
    check(normalize_name("特  力Ａ") == "特力A", f"NFKC/space: {normalize_name('特  力Ａ')!r}")
    check(normalize_name("XDST嘉澳") == "ST嘉澳", f"XD strip: {normalize_name('XDST嘉澳')!r}")
    check(looks_st(normalize_name("XDST嘉澳")), "ST must survive XD stripping")
    check(not looks_st(normalize_name("XD恒兴新")), "false ST on a plain XD name")

    # --- board / BSE guards ----------------------------------------------- #
    check(board_of("600519") == "sh_main", "board 600")
    check(board_of("300530") == "chinext", "board 300")
    check(board_of("688456") == "star", "board 688")
    check(board_of("920271") is None, "BSE 920 must not map to a board")
    check(is_bse("920271.BJ", "920271"), "BSE suffix detection")
    check(symbol6("002409.SZ") == "002409", "symbol6 strips suffix")

    # --- calendar envelope ------------------------------------------------ #
    cal = {
        "tool": "trade_calendar",
        "result": [
            {"cal_date": "2026-10-08", "market_state": "休市-国庆"},
            {"cal_date": "2026-10-09", "market_state": "开市"},
            {"cal_date": "2026-10-10", "market_state": "休市-周末"},
            {"cal_date": "2026-10-12", "market_state": "开市-半日市"},
        ],
    }
    check(calendar_dates_from_envelope(cal) == ["2026-10-09", "2026-10-12"],
          f"calendar filter: {calendar_dates_from_envelope(cal)}")
    # measured upstream shape: trading_date=YYYYMMDD, market_state="休市（周末休市）"
    cal_real = {
        "tool": "trade_calendar",
        "result": [
            {"trading_date": "20261020", "market_state": "开市"},
            {"trading_date": "20261024", "market_state": "休市（周末休市）"},
            {"trading_date": "20261026", "market_state": "开市"},
        ],
    }
    check(calendar_dates_from_envelope(cal_real) == ["2026-10-20", "2026-10-26"],
          f"trading_date shape: {calendar_dates_from_envelope(cal_real)}")

    # --- board split merge (a session over the 100-row cap) --------------- #
    def mk_part(board: str, code: str, total: int = 1) -> tuple[list[dict[str, str]], int, dict[str, Any]]:
        return (
            [{
                "股票代码": code,
                "股票名称": "测试",
                "收盘价(元)": "10.00",
                "涨停价(元)": "10.00",
                "是否ST": "否",
            }],
            total,
            {"trace_id": board},
        )

    ok_parts = {
        "sh_main": mk_part("sh_main", "600001"),
        "sz_main": mk_part("sz_main", "000001"),
        "chinext": mk_part("chinext", "300001"),
        "star": mk_part("star", "688001"),
    }
    merged = build_pool_from_parts(
        trade_date="2026-06-01", parts=ok_parts, fetched_at="2026-10-26T02:30:00+08:00"
    )
    check(merged["status"] == "ok", f"split merge status: {merged['status']} {merged.get('availability_reason')}")
    check(merged["count"] == 4, f"split merge count: {merged['count']}")
    check(merged["codes"] == ["000001", "300001", "600001", "688001"],
          f"split merge codes: {merged['codes']}")

    # a board that legitimately had no limit-up must not fail the merge
    zero_parts = dict(ok_parts)
    zero_parts["star"] = ([], 0, {"trace_id": "star"})
    zeroed = build_pool_from_parts(
        trade_date="2026-06-01", parts=zero_parts, fetched_at="2026-10-26T02:30:00+08:00"
    )
    check(zeroed["status"] == "ok", f"empty board must be legal: {zeroed.get('availability_reason')}")
    check(zeroed["count"] == 3, f"empty board count: {zeroed['count']}")

    # a board that was never queried is unknown, not zero
    missing_parts = dict(ok_parts)
    missing_parts.pop("star")
    missing = build_pool_from_parts(
        trade_date="2026-06-01", parts=missing_parts, fetched_at="2026-10-26T02:30:00+08:00"
    )
    check(missing["status"] == "error", "missing board must not pass as a smaller pool")
    check("star:not_fetched" in str(missing.get("availability_reason")),
          f"missing board reason: {missing.get('availability_reason')}")

    # a part that is itself truncated must still be caught
    trunc_parts = dict(ok_parts)
    trunc_parts["sh_main"] = mk_part("sh_main", "600001", total=150)
    truncated = build_pool_from_parts(
        trade_date="2026-06-01", parts=trunc_parts, fetched_at="2026-10-26T02:30:00+08:00"
    )
    check(truncated["status"] == "error", "truncated part must fail the merge")
    check(truncated["codes"] == [], "truncated merge must not emit partial codes")

    # --- end-to-end on a temp cache dir ----------------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        (cache / "neodata_raw").mkdir()
        atomic_write_json(
            cache / "neodata_raw" / "calendar_0001.json",
            {
                "tool": "trade_calendar",
                "fetched_at": "2026-10-26T02:20:00+08:00",
                "params": {},
                "result": [
                    {"cal_date": "2026-10-08", "market_state": "休市-国庆"},
                    {"cal_date": "2026-10-09", "market_state": "开市"},
                    {"cal_date": "2026-10-12", "market_state": "开市"},
                ],
            },
        )
        atomic_write_json(
            cache / "neodata_raw" / "limit_up_2026-10-09.json",
            _csv_envelope(blob, 6),
        )
        args = argparse.Namespace(
            cache_dir=str(cache), raw_dir=str(cache / "neodata_raw"),
            as_of="2026-10-12", sessions=2, out=None, strict=False,
        )
        code = cmd_ingest(args)
        check(code == 0, f"ingest exit code: {code}")
        pool = read_json(cache / "limit_up_pool" / "2026-10-09.json") or {}
        check(pool.get("count") == 3, f"ingest pool count: {pool.get('count')}")
        calendar = read_json(cache / "calendar.json") or []
        check(len(calendar) == 2, f"calendar truncation: {len(calendar)} rows")
        check(read_json(cache / "neodata_limit_up_manifest.json") is not None, "manifest missing")
        # idempotency: a second ingest must not change the store
        before = (cache / "limit_up_pool" / "2026-10-09.json").read_text(encoding="utf-8")
        cmd_ingest(args)
        after = (cache / "limit_up_pool" / "2026-10-09.json").read_text(encoding="utf-8")
        check(before == after, "ingest is not idempotent")
        # plan must see 2026-10-12 as the only pending session
        plan_args = argparse.Namespace(
            cache_dir=str(cache), raw_dir=str(cache / "neodata_raw"),
            as_of="2026-10-12", sessions=2, out=str(cache / "plan.jsonl"),
        )
        check(cmd_plan(plan_args) == 0, "plan failed on the temp cache")
        lines = (cache / "plan.jsonl").read_text(encoding="utf-8").strip().splitlines()
        check(len(lines) == 1, f"plan pending count: {len(lines)}")
        check(json.loads(lines[0])["trade_date"] == "2026-10-12", "plan picked the wrong session")

    print(f"selftest: {'PASS' if not failures else 'FAIL'} ({len(failures)} failures)")
    for failure in failures:
        print(f"  - {failure}")
    return 0 if not failures else 1


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize only the limit-up pool + trading calendar from NeoData envelopes.",
    )
    parser.add_argument("--selftest", action="store_true", help="run offline assertions and exit")
    sub = parser.add_subparsers(dest="command")

    plan = sub.add_parser("plan", help="emit the per-session call list (JSONL) still to be fetched")
    plan.add_argument("--cache-dir", required=True)
    plan.add_argument("--raw-dir")
    plan.add_argument("--as-of")
    plan.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS)
    plan.add_argument("--out")
    plan.set_defaults(func=cmd_plan)

    ingest = sub.add_parser("ingest", help="convert raw envelopes into the v4 limit_up_pool contract")
    ingest.add_argument("--cache-dir", required=True)
    ingest.add_argument("--raw-dir", required=True)
    ingest.add_argument("--as-of")
    ingest.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS)
    ingest.add_argument("--strict", action="store_true", help="exit 1 unless every session is ok")
    ingest.set_defaults(func=cmd_ingest)

    status = sub.add_parser("status", help="report per-session pool_status for the window")
    status.add_argument("--cache-dir", required=True)
    status.add_argument("--as-of")
    status.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS)
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
