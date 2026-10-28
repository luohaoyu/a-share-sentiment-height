#!/usr/bin/env python3
"""Screen all qualified A-shares whose N-session return reaches a threshold."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from compute_market_winners_from_daily import (
    GAIN_BANDS,
    load_calendar,
    first_value,
    load_master,
    load_price_map,
    load_st,
    market_for,
    normalize_code,
    normalize_date,
    numeric,
    read_records,
    risk_name,
)

SOURCE_MARKETS = ["沪市主板", "深市主板", "创业板", "科创板"]
MARKET_GROUPS = {
    "沪深主板": ["沪市主板", "深市主板"],
    "科创成长板": ["科创板", "创业板"],
}


def listing_status_is_active(info: dict[str, Any]) -> bool:
    raw = str(info.get("list_status") or info.get("listing_status") or info.get("status") or "L").strip().upper()
    return raw in {"L", "LISTED", "上市", "正常上市"}


def select_strong_pool(
    day_rows: list[dict[str, Any]],
    rank_leg_size: int,
    min_return: float,
) -> tuple[list[dict[str, Any]], int]:
    """强势股池 = {当日10日涨幅 ≥ min_return} ∪ {当日10日涨幅排名前 rank_leg_size}。

    day_rows 必须已按 pct 降序，达标集因此必为该序列的前缀，于是并集等价于
    “取 score 最高的一批、数量 = max(排行腿宽度, 达标只数)”。这样两处消费
    （昨日池→情绪K线信号、当日池→阈值池与 rank_fill 补位）共用同一口径，
    不会再出现“信号K按纯排行、阈值池按纯阈值”两套规则并存。
    """
    threshold_members = 0
    for row in day_rows:
        if float(row["pct"]) + 1e-10 >= min_return:
            threshold_members += 1
        else:
            break
    take = max(rank_leg_size, threshold_members)
    return day_rows[:take], threshold_members


def load_ohlc_maps(path: Path) -> tuple[
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
]:
    """Parse daily partitions once and build all four OHLC maps.

    The previous path called load_price_map four times, reopening and decoding every
    full-market JSON partition once per field. This preserves the same duplicate
    conflict checks while reducing JSON parsing from four passes to one.
    """
    maps: dict[str, dict[tuple[str, str], float]] = {
        "open": {}, "high": {}, "low": {}, "close": {},
    }
    candidates = {
        "open": ("open", "adj_open"),
        "high": ("high", "adj_high"),
        "low": ("low", "adj_low"),
        "close": ("close", "adj_close"),
    }
    for row in read_records(path):
        code = normalize_code(first_value(row, "ts_code", "code", "symbol"))
        raw_date = first_value(row, "trade_date", "date", "cal_date")
        if not code or raw_date in (None, ""):
            continue
        key = (code, normalize_date(raw_date))
        for field, value_keys in candidates.items():
            raw_value = first_value(row, *value_keys)
            if raw_value in (None, ""):
                continue
            value = numeric(raw_value, field)
            previous = maps[field].get(key)
            if previous is not None and not math.isclose(previous, value, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f"conflicting duplicate {field} for {key}: {previous} vs {value}")
            maps[field][key] = value
    return maps["open"], maps["high"], maps["low"], maps["close"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen full-market N-session return threshold gainers")
    parser.add_argument("--calendar", required=True, type=Path)
    parser.add_argument("--daily", required=True, type=Path)
    parser.add_argument("--adj-factor", type=Path)
    parser.add_argument("--stock-basic", required=True, type=Path)
    parser.add_argument("--st", type=Path)
    parser.add_argument("--st-coverage", choices=("complete", "partial"), default="complete")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-count", type=int, default=30)
    parser.add_argument("--lookback", type=int, default=10)
    parser.add_argument("--min-return", type=float, default=50.0)
    parser.add_argument(
        "--strong-pool-rank-size", "--signal-pool-size", dest="strong_pool_rank_size",
        type=int, default=10,
        help=(
            "强池排行腿宽度N（默认10）。强势股池 = {10日涨幅≥min-return} ∪ {按10日涨幅排名前N}："
            "达标股全收，不足N只才用排行补。同一口径两处消费：① 情绪K线信号池（取昨日池）；"
            "② 阈值池与 rank_fill 补位（取当日池）。--signal-pool-size 为兼容别名，语义完全相同"
        ),
    )
    parser.add_argument("--threshold-days", type=int, default=60)
    parser.add_argument("--end-date")
    parser.add_argument("--prices-adjusted", action="store_true")
    parser.add_argument("--allow-name-only-st", action="store_true")
    parser.add_argument("--markets", nargs="*", default=SOURCE_MARKETS, choices=SOURCE_MARKETS)
    args = parser.parse_args()

    if args.target_count <= 0 or args.lookback <= 0 or args.strong_pool_rank_size <= 0 or args.threshold_days < 0:
        parser.error("target-count/lookback/strong-pool-rank-size must be positive and threshold-days cannot be negative")
    if not math.isfinite(args.min_return):
        parser.error("min-return must be finite")
    if not args.prices_adjusted and args.adj_factor is None:
        parser.error("--adj-factor is required unless --prices-adjusted is set")
    if args.st is None and not args.allow_name_only_st:
        parser.error("--st is required; use --allow-name-only-st only as an explicit degraded mode")
    if args.st_coverage == "partial" and not args.allow_name_only_st:
        parser.error("partial ST coverage requires --allow-name-only-st")

    calendar = load_calendar(args.calendar)
    if args.end_date:
        cutoff = normalize_date(args.end_date)
        calendar = [item for item in calendar if item <= cutoff]
    # 额外保留一个交易日，用于给最早目标日构建“昨日强势股”10日收益排名。
    required = args.target_count + args.lookback + 1
    if len(calendar) < required:
        raise ValueError(f"calendar contains {len(calendar)} completed sessions; need {required}")
    selected = calendar[-required:]
    target_dates = selected[-args.target_count:]
    base_date_by_target = {
        selected[index]: selected[index - args.lookback]
        for index in range(args.lookback, len(selected))
    }
    previous_date_by_target = {
        selected[index]: selected[index - 1]
        for index in range(args.lookback, len(selected))
    }

    master = load_master(args.stock_basic)
    opens, highs, lows, closes = load_ohlc_maps(args.daily)
    factors = {} if args.prices_adjusted else load_price_map(args.adj_factor, ("adj_factor", "factor"), "adj_factor")
    st_keys, st_names = load_st(args.st)
    # 最早目标日也需要前一交易日的强势股池，因此收益候选须多计算一个会话。
    target_set = set(target_dates)
    signal_selection_dates = {previous_date_by_target[day] for day in target_dates}
    computation_dates = target_set | signal_selection_dates
    qualified: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    eligible_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    quality = Counter()

    for (code, trade_date), close in closes.items():
        if trade_date not in computation_dates:
            continue
        quality["target_price_rows"] += 1
        base_date = base_date_by_target[trade_date]
        base_close = closes.get((code, base_date))
        if base_close is None or base_close <= 0 or close <= 0:
            quality["missing_or_invalid_base_close"] += 1
            continue
        if args.prices_adjusted:
            adjusted_close = close
            adjusted_base = base_close
        else:
            factor = factors.get((code, trade_date))
            base_factor = factors.get((code, base_date))
            if factor is None or base_factor is None or factor <= 0 or base_factor <= 0:
                quality["missing_or_invalid_factor"] += 1
                continue
            adjusted_close = close * factor
            adjusted_base = base_close * base_factor
        pct = (adjusted_close / adjusted_base - 1.0) * 100.0
        if not math.isfinite(pct):
            quality["non_finite_return"] += 1
            continue
        quality["return_candidates"] += 1
        threshold_hit = pct + 1e-10 >= args.min_return
        if threshold_hit:
            quality["threshold_hits_before_qualification"] += 1
        info = master.get(code)
        if info is None:
            quality["missing_stock_master"] += 1
            excluded.append({
                "date": trade_date, "base_date": base_date, "ts_code": code,
                "name": code, "pct": round(pct, 6), "market": None,
                "exclusion_reason": "股票主数据缺失",
            })
            continue
        market = market_for(code, info)
        if market not in args.markets:
            quality["outside_configured_markets"] += 1
            continue
        display_name = st_names.get((code, trade_date)) or str(info.get("name") or code)
        reason = ""
        active_listing = listing_status_is_active(info)
        st = (code, trade_date) in st_keys or risk_name(display_name) or not active_listing
        if (code, trade_date) in st_keys:
            reason = "历史ST/风险快照命中"
        elif risk_name(display_name):
            reason = "名称带ST/*ST/退市风险标识"
        elif not active_listing:
            reason = "股票主数据不是正常上市状态"
        listing_date = info.get("listing_date")
        age_days = None
        new_stock = False
        if not listing_date:
            new_stock = True
            reason = reason or "上市日期无法核验"
        else:
            age_days = (date.fromisoformat(trade_date) - date.fromisoformat(listing_date)).days
            if age_days < args.threshold_days:
                new_stock = True
                reason = reason or f"上市{age_days}个自然日，小于{args.threshold_days}日阈值"
        row = {
            "date": trade_date,
            "base_date": base_date,
            "market": market,
            "market_group": "沪深主板" if market in MARKET_GROUPS["沪深主板"] else "科创成长板",
            "code": code.split(".", 1)[0],
            "ts_code": code,
            "name": display_name,
            "pct": round(pct, 6),
            "close": close,
            "base_close": base_close,
            "listing_date": listing_date,
            "listing_age_days": age_days,
            "st": st,
            "new": new_stock,
        }
        if st or new_stock:
            if threshold_hit:
                row["exclusion_reason"] = reason
                excluded.append(row)
        else:
            eligible_by_date[trade_date].append(row)
            if threshold_hit and trade_date in target_set:
                qualified.append(row)

    qualified.sort(key=lambda row: (row["date"], -row["pct"], row["ts_code"]))
    excluded.sort(key=lambda row: (row["date"], -row["pct"], row["ts_code"]))
    counts_by_date = Counter(row["date"] for row in qualified)
    counts_by_market = Counter(row["market"] for row in qualified)
    unique_codes = sorted({row["ts_code"] for row in qualified})
    daily_summary = []
    # 阈值补位（rank_fill）：当日合格池按 10 日涨幅排序取前 N，剔除已达标的，
    # 用于补足阈值池在弱势交易日样本过薄的缺口。**只写入独立数组，绝不并入 qualified**，
    # 否则报告层 build_height 的 "count": len(scored) 会把补位股计入广度，导致
    # 每日达标家数由 3~19 被抬平为 10~19，"扩张/收缩"叙述与 state 判定随之失真。
    rank_fill: list[dict[str, Any]] = []
    previous_leader_code = ""
    leader_streak_days = 0
    for trade_date in target_dates:
        rows = [row for row in qualified if row["date"] == trade_date]
        eligible_rows = sorted(eligible_by_date.get(trade_date, []), key=lambda row: (-row["pct"], row["ts_code"]))
        leader = eligible_rows[0] if eligible_rows else None
        leader_code = str(leader.get("ts_code")) if leader else ""
        leader_changed = bool(previous_leader_code and leader_code and leader_code != previous_leader_code)
        if leader_code and leader_code == previous_leader_code:
            leader_streak_days += 1
        elif leader_code:
            leader_streak_days = 1
        else:
            leader_streak_days = 0
        top3 = eligible_rows[:3]
        top3_values = [float(row["pct"]) for row in top3]

        market_maxima = {}
        for market in args.markets:
            market_rows = [row for row in eligible_rows if row["market"] == market]
            if market_rows:
                market_maxima[market] = market_rows[0]
        market_group_maxima = {}
        for group, source_markets in MARKET_GROUPS.items():
            group_rows = [row for row in eligible_rows if row["market"] in source_markets]
            if group_rows:
                market_group_maxima[group] = group_rows[0]

        # 情绪 K 线信号：全市场及每个市场分组都独立在昨日收盘后按10日涨幅
        # 取前N并固定股票池，今日观察该池相对昨收的等权平均OHLC表现。
        # 每根K线分别锚定今日对应范围的10日涨幅最大值。
        previous_date = previous_date_by_target[trade_date]
        previous_eligible_rows = sorted(
            eligible_by_date.get(previous_date, []),
            key=lambda row: (-row["pct"], row["ts_code"]),
        )

        def build_signal_candle(
            pool_candidates: list[dict[str, Any]],
            height_leader: dict[str, Any] | None,
            scope: str,
            market_group: str | None = None,
        ) -> dict[str, Any] | None:
            selected_pool, threshold_members = select_strong_pool(
                pool_candidates, args.strong_pool_rank_size, args.min_return
            )
            members = []
            for candidate in selected_pool:
                code = str(candidate["ts_code"])
                previous_close = closes.get((code, previous_date))
                day_open = opens.get((code, trade_date))
                day_high = highs.get((code, trade_date))
                day_low = lows.get((code, trade_date))
                day_close = closes.get((code, trade_date))
                prices = (previous_close, day_open, day_high, day_low, day_close)
                if any(value is None or not math.isfinite(float(value)) for value in prices) or float(previous_close) <= 0:
                    quality["signal_pool_missing_ohlc"] += 1
                    continue
                returns = {
                    "open": float(day_open) / float(previous_close) - 1.0,
                    "high": float(day_high) / float(previous_close) - 1.0,
                    "low": float(day_low) / float(previous_close) - 1.0,
                    "close": float(day_close) / float(previous_close) - 1.0,
                }
                members.append({
                    "ts_code": code,
                    "code": candidate["code"],
                    "name": candidate["name"],
                    "market": candidate["market"],
                    "market_group": candidate["market_group"],
                    "selection_date": previous_date,
                    "selection_return_10d": round(float(candidate["pct"]) / 100.0, 8),
                    **{f"{key}_return": round(value, 8) for key, value in returns.items()},
                })
            if not height_leader or not members:
                return None
            base_height = float(height_leader["pct"]) / 100.0
            mean_returns = {
                key: sum(float(member[f"{key}_return"]) for member in members) / len(members)
                for key in ("open", "high", "low", "close")
            }
            # 池质量：成员 10 日涨幅中位数。用于区分“K线弱是因为池本来就弱”与“池够强但次日承接差”。
            pool_returns = sorted(float(member["selection_return_10d"]) for member in members)
            mid = len(pool_returns) // 2
            pool_median_return = (
                pool_returns[mid]
                if len(pool_returns) % 2 == 1
                else (pool_returns[mid - 1] + pool_returns[mid]) / 2.0
            )
            scope_text = market_group or "全市场"
            return {
                "kind": "height_anchored_previous_strong_pool_signal",
                "scope": scope,
                "market_group": market_group,
                "pool_definition": (
                    f"previous_day_{scope}_strong_pool_ge_{args.min_return:g}pct"
                    f"_union_top_{args.strong_pool_rank_size}_by_{args.lookback}d_return"
                ),
                "selection_date": previous_date,
                "performance_date": trade_date,
                "requested_pool_size": max(args.strong_pool_rank_size, threshold_members),
                "selected_pool_size": len(selected_pool),
                "valid_pool_size": len(members),
                "rank_leg_size": args.strong_pool_rank_size,
                "threshold_members": threshold_members,
                "rank_fill_members": len(selected_pool) - threshold_members,
                "pool_median_return_10d": round(pool_median_return, 8),
                "coverage_status": "complete" if len(members) == len(selected_pool) else "partial",
                "base_height_10d": round(base_height, 8),
                "return_offsets": {key: round(value, 8) for key, value in mean_returns.items()},
                "display": {key: round(base_height + value, 8) for key, value in mean_returns.items()},
                "members": members,
                "note": (
                    f"K线为{scope_text}昨日强势股次日表现信号：强势股池 = 昨日10日涨幅≥{args.min_return:g}% 的全部股票，"
                    f"不足{args.strong_pool_rank_size}只时按昨日10日涨幅排名补足前{args.strong_pool_rank_size}名"
                    f"（本次入池{len(selected_pool)}只，其中达标{threshold_members}只）；"
                    "OHLC为其今日相对昨收的等权平均收益；纵轴锚点仍为今日该范围10日涨幅最大值，"
                    "不代表真实10日涨幅OHLC。"
                ),
            }

        signal_candle = build_signal_candle(previous_eligible_rows, leader, "all_market")
        market_group_signal_candles = {}
        for group, source_markets in MARKET_GROUPS.items():
            group_previous_rows = [row for row in previous_eligible_rows if row["market"] in source_markets]
            group_leader = market_group_maxima.get(group)
            group_signal = build_signal_candle(group_previous_rows, group_leader, "market_group", group)
            if group_signal:
                market_group_signal_candles[group] = group_signal

        # 当日强池的排行腿：合格池已按 10 日涨幅降序（eligible_rows），取前 N；其中不达标的部分
        # 即 rank_fill。达标数 >= N 时该集合必为空（前 N 名全在 qualified 内，属严格子集），零增量；
        # 只有达标数 < N 的弱势日才产生补位，补位股 pct 天然低于阈值。
        # 与信号K共用「阈值∪排行」同一口径，区别只在锚定日（此处锚定当日，信号K锚定昨日）。
        qualified_codes_today = {str(row["ts_code"]) for row in rows}
        fill_today: list[dict[str, Any]] = []
        for rank, candidate in enumerate(eligible_rows[: args.strong_pool_rank_size], start=1):
            candidate_code = str(candidate["ts_code"])
            if candidate_code in qualified_codes_today:
                continue
            fill_today.append({
                **candidate,
                "source": "rank_fill",
                "selection_rank": rank,
                "fill_reason": (
                    f"当日按10日涨幅排名第{rank}，涨幅{round(float(candidate['pct']), 2)}%"
                    f"未达{args.min_return:g}%阈值，按前{args.strong_pool_rank_size}名补位"
                ),
            })
        rank_fill.extend(fill_today)

        daily_summary.append({
            "date": trade_date,
            "base_date": base_date_by_target[trade_date],
            "qualified_count": len(rows),
            "eligible_count": len(eligible_rows),
            "rank_fill_count": len(fill_today),
            "leader": leader,
            "maximum_return_10d": round(float(leader["pct"]), 6) if leader else None,
            "leader_changed": leader_changed,
            "leader_streak_days": leader_streak_days,
            "top3": top3,
            "top3_median_return_10d": round(sorted(top3_values)[len(top3_values) // 2], 6) if top3_values else None,
            "emotion_candle_signal": signal_candle,
            "market_group_emotion_candle_signals": market_group_signal_candles,
            "market_maxima": market_maxima,
            "market_group_maxima": market_group_maxima,
        })
        previous_leader_code = leader_code or previous_leader_code
    for index, item in enumerate(daily_summary):
        current = item.get("maximum_return_10d")
        previous = daily_summary[index - 1].get("maximum_return_10d") if index else None
        item["maximum_change_vs_previous"] = round(float(current) - float(previous), 6) if current is not None and previous is not None else None

    warnings = []
    if args.st is None:
        warnings.append("未提供历史ST快照，仅按股票主数据名称过滤；历史风险状态可能漏判。")
    elif args.st_coverage == "partial":
        warnings.append("历史ST快照仅部分覆盖；未覆盖日期仅按名称过滤，可能漏判历史风险状态。")
    failed_symbols = 0
    manifest_path = args.daily.parent / "bootstrap_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            failed_symbols = int(manifest.get("summary", {}).get("failed_symbols", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            warnings.append("bootstrap manifest 无法解析，无法确认历史取数失败股票数。")
    failed_risk_only = 0
    if failed_symbols and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            failed_codes = {
                str(row.get("ts_code"))
                for row in manifest.get("summary", {}).get("failures", [])
                if row.get("ts_code")
            }
            failed_risk_only = sum(
                1 for code in failed_codes
                if code in master and risk_name(str(master[code].get("name") or ""))
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            failed_risk_only = 0
    effective_failed_symbols = max(0, failed_symbols - failed_risk_only)
    if failed_risk_only:
        warnings.append(f"历史回填失败的 {failed_risk_only} 只股票当前名称均为ST/风险标识，已按资格规则先验排除。")
    if effective_failed_symbols:
        warnings.append(f"历史回填仍有 {effective_failed_symbols} 只可能合格股票失败，结果不构成完整全市场横截面。")

    output = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "title": "短线情绪高度",
        "subtitle": "基于沪深主板与科创成长板每日10日涨幅最大值，观察短线情绪高度的扩张、分化与收缩",
        "dates": target_dates,
        "markets": args.markets,
        "market_groups": MARKET_GROUPS,
        "excluded_markets": ["北交所", "其他非沪市主板、深市主板、科创板、创业板市场"],
        "selection_mode": "daily_maximum_with_threshold_breadth",
        "primary_business_goal": "track_short_term_sentiment_height_by_daily_maximum_10d_return",
        "lookback_trading_sessions": args.lookback,
        "return_formula": "adjusted_close(t) / adjusted_close(t-lookback) - 1",
        "adjustment_mode": "adjusted_close_input" if args.prices_adjusted else "raw_close_times_adj_factor",
        "min_return_pct": args.min_return,
        "signal_candle": {
            "enabled": True,
            "rank_leg_size": args.strong_pool_rank_size,
            "threshold_pct": args.min_return,
            "pool_definition": (
                f"strong_pool = {{previous_day_{args.lookback}d_return >= {args.min_return:g}%}}"
                f" union {{previous_day_top_{args.strong_pool_rank_size}_by_{args.lookback}d_return}}, "
                "evaluated independently within each scope"
            ),
            "pool_selection": (
                f"previous_day_strong_pool_ge_{args.min_return:g}pct_union_top_"
                f"{args.strong_pool_rank_size}_by_{args.lookback}d_return_after_qualification_within_each_scope"
            ),
            "scopes": ["all_market", *MARKET_GROUPS.keys()],
            "selection_timing": "previous_trading_day_close",
            "performance_window": "next_trading_day_ohlc_vs_previous_close",
            "anchor": "performance_day_scope_maximum_return_10d",
            "formula": "display_ohlc(scope) = performance_day_scope_maximum_return_10d + equal_weight_mean((performance_day_ohlc - selection_day_close) / selection_day_close, members selected within scope)",
            "semantics": "height_anchored_previous_strong_pool_next_day_signal_not_real_10d_ohlc",
        },
        "new_stock_threshold_days": args.threshold_days,
        "qualified": qualified,
        "qualified_rows": len(qualified),
        "unique_qualified_stocks": len(unique_codes),
        "unique_qualified_codes": unique_codes,
        "rank_fill": rank_fill,
        "rank_fill_rows": len(rank_fill),
        "rank_fill_unique_stocks": len({str(row["ts_code"]) for row in rank_fill}),
        "rank_fill_rule": {
            "enabled": True,
            "rank_leg_size": args.strong_pool_rank_size,
            "threshold_pct": args.min_return,
            "scope": "daily_strong_pool_rank_leg_after_qualification_within_configured_markets",
            "selection_date": "same_day",
            "excludes_qualified_members": True,
            "note": (
                "rank_fill 是「阈值∪排行」强池在当日产生的那部分排行腿记录，独立于 qualified，"
                "不写入 qualified、不参与 qualified_count 与 unique_qualified_codes，"
                "因此报告的高度曲线、榜首、广度家数均不受影响。"
            ),
        },
        "daily_summary": daily_summary,
        "counts_by_market": dict(counts_by_market),
        "audit_excluded": excluded,
        "audit_excluded_count": len(excluded),
        "quality": dict(sorted(quality.items())),
        "source": {
            "calendar": str(args.calendar),
            "daily": str(args.daily),
            "adj_factor": str(args.adj_factor) if args.adj_factor else None,
            "stock_basic": str(args.stock_basic),
            "st": str(args.st) if args.st else None,
            "st_coverage": args.st_coverage if args.st else "none",
            "bootstrap_manifest": str(manifest_path) if manifest_path.exists() else None,
            "bootstrap_failed_symbols": failed_symbols,
            "bootstrap_failed_risk_only": failed_risk_only,
            "bootstrap_effective_failed_symbols": effective_failed_symbols,
        },
        "trend_ui": {
            "default_series": ["总体极值", "沪深主板", "科创成长板"],
            "available_market_groups": list(MARKET_GROUPS),
            "gain_bands": GAIN_BANDS,
        },
        "process_log": [
            {
                "step": 1,
                "component": "Skill/Agent 决策层",
                "purpose": "冻结每日10日涨幅最大值为短线情绪高度主指标，50%阈值数量作为广度验证，并确定资格规则",
                "status": "完成",
            },
            {
                "step": 2,
                "component": "AkShare 数据接入脚本",
                "purpose": "获取交易日历、股票主数据和后复权短窗口行情并落盘缓存；计算层仅纳入沪市主板、深市主板、科创板和创业板",
                "status": "完成" if failed_symbols == 0 else "部分完成",
            },
            {
                "step": 3,
                "component": "本地情绪高度计算脚本",
                "purpose": "逐日计算四板块10交易日收益，排除ST、*ST、退市、退市整理和新股，提取总体、沪深主板及科创成长板最大值并统计阈值广度",
                "status": "完成",
            },
            {
                "step": 4,
                "component": "校验与报告脚本",
                "purpose": "核对阈值、日期、重复、过滤资格和逐日汇总，并渲染HTML",
                "status": "待校验",
            },
        ],
        "warning": "；".join(warnings) if warnings else "无额外数据质量警告。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "dates": len(target_dates),
        "qualified_rows": len(qualified),
        "unique_stocks": len(unique_codes),
        "excluded_threshold_hits": len(excluded),
        "bootstrap_failed_symbols": failed_symbols,
        "bootstrap_failed_risk_only": failed_risk_only,
        "bootstrap_effective_failed_symbols": effective_failed_symbols,
    }, ensure_ascii=False))
    return 0 if effective_failed_symbols == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
