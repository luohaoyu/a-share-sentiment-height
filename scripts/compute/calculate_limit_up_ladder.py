#!/usr/bin/env python3
"""Calculate the daily limit-up ladder (涨停家数 / 连板梯队 / 最高连板).

Offline only. Reads the authoritative price store (``price_store/<ts_code>.json``)
because only that layer carries ``qfq[].is_limit_up``; ``daily_adjusted/`` has no
limit-up field at all.

Scope (frozen by the caller, not inferred here):
    * 沪市主板 / 深市主板 / 创业板 / 科创板 only (北交所 excluded)
    * ST / *ST / 退市风险 names excluded
    * listings younger than ``--min-listing-days`` excluded
    * 连板 = consecutive sessions with ``is_limit_up is True``

Known limits, surfaced in the output instead of being papered over:
    * the earliest cached session cannot know its predecessor, so any streak that
      starts there is a *lower bound* (``window.lower_bound_before``)
    * a missing row / ``is_limit_up is None`` is *unknown*, not ``False``; it is
      counted in ``audit.unknown_rows`` and does not break a streak (a suspended
      stock resuming into another limit-up keeps its board count)
    * ST 5% board ratio is not implemented upstream, which is why ST is excluded

Usage:
    python calculate_limit_up_ladder.py \
        --price-store <cache>/price_store \
        --calendar <cache>/calendar.json \
        --stock-basic <cache>/stock_basic.json \
        --end-date 2026-10-26 --window-days 30 \
        --output <run>/limit_up_ladder.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from compute_market_winners_from_daily import (
    load_calendar,
    load_master,
    market_for,
    normalize_code,
    normalize_date,
    risk_name,
)

SCHEMA_VERSION = 1
SOURCE_MARKETS = ["沪市主板", "深市主板", "创业板", "科创板"]
MARKET_GROUPS = {
    "沪深主板": ["沪市主板", "深市主板"],
    "科创成长板": ["科创板", "创业板"],
}
GROUP_OF_MARKET = {market: group for group, markets in MARKET_GROUPS.items() for market in markets}

BASIS = "收盘涨停：本地日K按板块涨跌幅限制判定（主板10% / 创业板20% / 科创板20%）"
ST_RATIO_NOTE = "ST 5% 涨跌幅限制上游未启用，ST/退市风险股按本口径整体排除"
MISSING_NOTE = "缺行或 is_limit_up 为 null 记为“数据不足”，不断板、不计入涨停"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def as_bool(value: Any) -> "bool | None":
    if value is True or value is False:
        return value
    return None


def load_store(directory: Path) -> tuple[dict[str, dict[str, bool]], dict[str, str], dict[str, str], Counter]:
    """Read every price store file with bounded parallel small-file I/O."""
    limit_by_date: dict[str, dict[str, bool]] = defaultdict(dict)
    list_date_by_code: dict[str, str] = {}
    name_by_code: dict[str, str] = {}
    stats: Counter = Counter()
    paths = sorted(directory.glob("*.json"))

    def parse(path: Path) -> tuple[str | None, str | None, list[tuple[str, bool]], Counter]:
        local: Counter = Counter()
        try:
            store = read_json(path)
        except (OSError, json.JSONDecodeError):
            local["unreadable_store"] += 1
            return None, None, [], local
        ts_code = normalize_code(store.get("ts_code") or path.stem)
        if not ts_code:
            local["bad_store_code"] += 1
            return None, None, [], local
        local["symbols"] += 1
        listing = None
        if store.get("list_date"):
            try:
                listing = normalize_date(store["list_date"])
            except ValueError:
                local["bad_list_date"] += 1
        parsed: list[tuple[str, bool]] = []
        rows = store.get("qfq") or []
        local["rows"] += len(rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                trade_date = normalize_date(row.get("trade_date"))
            except ValueError:
                local["bad_trade_date"] += 1
                continue
            flag = as_bool(row.get("is_limit_up"))
            if flag is None:
                local["unknown_rows"] += 1
                continue
            if flag:
                local["limit_up_rows"] += 1
            parsed.append((trade_date, flag))
        return ts_code, listing, parsed, local

    with ThreadPoolExecutor(max_workers=min(16, max(1, len(paths)))) as executor:
        for ts_code, listing, parsed, local in executor.map(parse, paths, chunksize=16):
            stats.update(local)
            if not ts_code:
                continue
            if listing:
                list_date_by_code[ts_code] = listing
            for trade_date, flag in parsed:
                limit_by_date[trade_date][ts_code] = flag
    return limit_by_date, list_date_by_code, name_by_code, stats


def resolve_identity(
    ts_code: str,
    master: dict[str, dict[str, Any]],
    list_date_by_code: dict[str, str],
) -> tuple[str, str, str, str | None]:
    """Return ``(market, group, name, listing_date)`` for one symbol."""
    info = master.get(ts_code) or {}
    market = market_for(ts_code, info) if info else None
    if market is None:
        market = market_for(ts_code, {})
    group = GROUP_OF_MARKET.get(str(market or ""), "")
    name = str(info.get("name") or ts_code.split(".", 1)[0])
    listing_date = info.get("listing_date") or list_date_by_code.get(ts_code)
    return str(market or ""), group, name, listing_date


def age_days(trade_date: str, listing_date: "str | None") -> "int | None":
    if not listing_date:
        return None
    try:
        return (date.fromisoformat(trade_date) - date.fromisoformat(listing_date)).days
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="计算每日涨停家数与连板梯队")
    parser.add_argument("--price-store", required=True, type=Path, help="price_store 目录（权威层）")
    parser.add_argument("--calendar", required=True, type=Path, help="calendar.json")
    parser.add_argument("--stock-basic", required=True, type=Path, help="stock_basic.json")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--end-date", default=None, help="截止交易日，默认缓存最新会话")
    parser.add_argument("--window-days", type=int, default=30, help="输出窗口交易日数")
    parser.add_argument("--min-listing-days", type=int, default=60, help="次新股排除阈值（自然日）")
    parser.add_argument("--ladder-min-boards", type=int, default=2, help="梯队明细保留的最低板数")
    parser.add_argument("--ladder-max-rows", type=int, default=120, help="单日梯队明细上限")
    args = parser.parse_args()

    if not args.price_store.is_dir():
        print(f"错误：price_store 不存在 {args.price_store}", file=sys.stderr)
        return 2
    if args.window_days <= 0:
        parser.error("--window-days 必须为正")

    limit_by_date, list_date_by_code, _, stats = load_store(args.price_store)
    if not limit_by_date:
        print("错误：price_store 未读到任何可用行情行", file=sys.stderr)
        return 2

    calendar = load_calendar(args.calendar)
    if args.end_date:
        cutoff = normalize_date(args.end_date)
        calendar = [item for item in calendar if item <= cutoff]

    store_sessions = sorted(limit_by_date)
    session_set = set(store_sessions)
    sessions = [item for item in calendar if item in session_set]
    if not sessions:
        sessions = store_sessions
    if args.end_date:
        sessions = [item for item in sessions if item <= normalize_date(args.end_date)]
    if len(sessions) < args.window_days:
        print(
            f"错误：缓存仅 {len(sessions)} 个交易日，不足窗口 {args.window_days} 日",
            file=sys.stderr,
        )
        return 2

    window = sessions[-args.window_days:]
    warmup = sessions[: -args.window_days]
    window_set = set(window)

    master = load_master(args.stock_basic)

    # 先给每只股票解析身份与静态资格（市场 / 名称），逐日资格再看上市天数
    identity: dict[str, tuple[str, str, str, str | None]] = {}
    in_scope: set[str] = set()
    excluded_market: Counter = Counter()
    for trade_date in window:
        for ts_code in limit_by_date[trade_date]:
            if ts_code in identity:
                continue
            market, group, name, listing_date = resolve_identity(ts_code, master, list_date_by_code)
            identity[ts_code] = (market, group, name, listing_date)
            if market in SOURCE_MARKETS:
                in_scope.add(ts_code)
            else:
                excluded_market[market or "未知市场"] += 1

    excluded_st: set[str] = set()
    for ts_code in in_scope:
        _, _, name, _ = identity[ts_code]
        if risk_name(name):
            excluded_st.add(ts_code)
    in_scope -= excluded_st

    streak: dict[str, int] = defaultdict(int)
    points: list[dict[str, Any]] = []
    audit_unknown_in_window = 0
    excluded_new: Counter = Counter()
    excluded_st_hits: Counter = Counter()
    previous_total: "int | None" = None
    previous_max: "int | None" = None

    for trade_date in sessions:
        day = limit_by_date[trade_date]
        boards_by_code: dict[str, int] = {}
        for ts_code, flag in day.items():
            if flag:
                streak[ts_code] += 1
                boards_by_code[ts_code] = streak[ts_code]
            else:
                streak[ts_code] = 0

        if trade_date not in window_set:
            continue

        by_boards: Counter = Counter()
        by_market: Counter = Counter()
        ladder: list[dict[str, Any]] = []
        total = 0
        truncated = False
        for ts_code, boards in boards_by_code.items():
            market, group, name, listing_date = identity.get(ts_code) or resolve_identity(
                ts_code, master, list_date_by_code
            )
            if market not in SOURCE_MARKETS:
                continue
            if ts_code in excluded_st:
                excluded_st_hits[trade_date] += 1
                continue
            age = age_days(trade_date, listing_date)
            if age is None or age < args.min_listing_days:
                excluded_new[trade_date] += 1
                continue
            total += 1
            by_boards[boards] += 1
            by_market[group] += 1
            if boards >= args.ladder_min_boards:
                ladder.append({
                    "boards": boards,
                    "code": ts_code.split(".", 1)[0],
                    "ts_code": ts_code,
                    "name": name,
                    "market": market,
                })
        ladder.sort(key=lambda row: (-row["boards"], row["ts_code"]))
        if len(ladder) > args.ladder_max_rows:
            ladder = ladder[: args.ladder_max_rows]
            truncated = True

        unknown_today = sum(
            1 for ts_code in in_scope if ts_code not in day
        )
        audit_unknown_in_window += unknown_today

        first_board = by_boards.get(1, 0)
        consecutive = total - first_board
        max_boards = max(by_boards) if by_boards else 0
        points.append({
            "date": trade_date,
            "total": total,
            "first_board": first_board,
            "consecutive": consecutive,
            "by_boards": {str(key): by_boards[key] for key in sorted(by_boards)},
            "max_boards": max_boards,
            "consecutive_ratio": round(consecutive / total, 6) if total else None,
            "by_market": {group: by_market.get(group, 0) for group in MARKET_GROUPS},
            "total_change": None if previous_total is None else total - previous_total,
            "max_boards_change": None if previous_max is None else max_boards - previous_max,
            "unknown_rows": unknown_today,
            "ladder": ladder,
            "ladder_truncated": truncated,
        })
        previous_total = total
        previous_max = max_boards

    first_session = window[0]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": date.today().isoformat(),
        "window": {
            "first_session": first_session,
            "last_session": window[-1],
            "sessions": len(window),
            "warmup_sessions": len(warmup),
            "warmup_first_session": warmup[0] if warmup else None,
            "lower_bound_before": first_session,
            "lower_bound_note": (
                f"{first_session} 及以前没有更早的缓存行，当日连板数只能视为下限；"
                "越靠近窗口起点，被低估的板数越多"
            ),
        },
        "scope": {
            "markets": list(SOURCE_MARKETS),
            "market_groups": MARKET_GROUPS,
            "exclude_bj": True,
            "exclude_st": True,
            "min_listing_days": args.min_listing_days,
            "basis": BASIS,
            "st_ratio_note": ST_RATIO_NOTE,
            "missing_note": MISSING_NOTE,
            "ladder_min_boards": args.ladder_min_boards,
        },
        "dates": window,
        "points": points,
        "audit": {
            "symbols_scanned": stats["symbols"],
            "rows_scanned": stats["rows"],
            "limit_up_rows": stats["limit_up_rows"],
            "unknown_rows": stats["unknown_rows"],
            "unknown_rows_in_window": audit_unknown_in_window,
            "unreadable_store": stats["unreadable_store"],
            "symbols_in_scope": len(in_scope),
            "symbols_excluded_market": sum(excluded_market.values()),
            "symbols_excluded_st": len(excluded_st),
            "by_excluded_market": dict(excluded_market),
            "excluded_st_limit_up_hits": sum(excluded_st_hits.values()),
            "excluded_new_limit_up_hits": sum(excluded_new.values()),
            "sessions_in_cache": len(sessions),
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    latest = points[-1]
    print(json.dumps({
        "output": str(args.output),
        "bytes": args.output.stat().st_size,
        "sessions": len(window),
        "latest_date": latest["date"],
        "latest_total": latest["total"],
        "latest_first_board": latest["first_board"],
        "latest_consecutive": latest["consecutive"],
        "latest_max_boards": latest["max_boards"],
        "latest_ladder_rows": len(latest["ladder"]),
        "symbols_in_scope": len(in_scope),
        "unknown_rows_in_window": audit_unknown_in_window,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
