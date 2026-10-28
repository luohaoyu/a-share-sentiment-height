#!/usr/bin/env python3
"""Validate full-market threshold-gainer output for consistency and completeness."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ALLOWED_MARKETS = {"沪市主板", "深市主板", "科创板", "创业板"}
MARKET_GROUPS = {
    "沪深主板": {"沪市主板", "深市主板"},
    "科创成长板": {"科创板", "创业板"},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate threshold gainers JSON")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--expected-dates", type=int, default=10)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    dates = data.get("dates", [])
    configured_markets = set(data.get("markets", []))
    if configured_markets != ALLOWED_MARKETS:
        errors.append(f"configured markets {sorted(configured_markets)} != allowed markets {sorted(ALLOWED_MARKETS)}")
    if data.get("market_groups") != {key: sorted(value) for key, value in MARKET_GROUPS.items()} and data.get("market_groups") != {key: list(value) for key, value in MARKET_GROUPS.items()}:
        supplied_groups = {key: set(value) for key, value in data.get("market_groups", {}).items()}
        if supplied_groups != MARKET_GROUPS:
            errors.append("market_groups definition mismatch")
    rows = data.get("qualified", [])
    excluded = data.get("audit_excluded", [])
    threshold = float(data.get("min_return_pct", 50.0))
    threshold_days = int(data.get("new_stock_threshold_days", 60))
    if len(dates) != args.expected_dates:
        errors.append(f"date count {len(dates)} != expected {args.expected_dates}")
    if dates != sorted(set(dates)):
        errors.append("dates are not unique ascending")
    keys = Counter((row.get("date"), row.get("ts_code")) for row in rows)
    duplicates = [key for key, count in keys.items() if count > 1]
    if duplicates:
        errors.append(f"duplicate date/code rows: {duplicates[:10]}")
    for row in rows:
        pct = float(row.get("pct"))
        if not math.isfinite(pct) or pct + 1e-10 < threshold:
            errors.append(f"below-threshold row: {row.get('date')} {row.get('ts_code')} {pct}")
        if row.get("date") not in dates:
            errors.append(f"row date outside target dates: {row.get('date')}")
        if row.get("st") or row.get("new"):
            errors.append(f"unqualified row included: {row.get('date')} {row.get('ts_code')}")
        if row.get("market") not in ALLOWED_MARKETS:
            errors.append(f"outside allowed market included: {row.get('date')} {row.get('ts_code')} {row.get('market')}")
        expected_group = next((group for group, markets in MARKET_GROUPS.items() if row.get("market") in markets), None)
        if row.get("market_group") != expected_group:
            errors.append(f"market group mismatch: {row.get('date')} {row.get('ts_code')}")
        listing = row.get("listing_date")
        if not listing:
            errors.append(f"missing listing date: {row.get('date')} {row.get('ts_code')}")
        elif (date.fromisoformat(row["date"]) - date.fromisoformat(listing)).days < threshold_days:
            errors.append(f"new stock included: {row.get('date')} {row.get('ts_code')}")
    if int(data.get("qualified_rows", -1)) != len(rows):
        errors.append("qualified_rows does not match qualified list length")
    if int(data.get("audit_excluded_count", -1)) != len(excluded):
        errors.append("audit_excluded_count does not match audit list length")
    daily = data.get("daily_summary", [])
    if len(daily) != len(dates):
        errors.append("daily_summary length does not match dates")
    actual_counts = Counter(row.get("date") for row in rows)
    previous_maximum = None
    for item in daily:
        day = item.get("date")
        if int(item.get("qualified_count", -1)) != actual_counts[day]:
            errors.append(f"daily count mismatch: {day}")
        leader = item.get("leader")
        maximum = item.get("maximum_return_10d")
        if not leader or maximum is None:
            errors.append(f"missing daily maximum or leader: {day}")
            continue
        leader_pct = float(leader.get("pct"))
        if not math.isclose(float(maximum), leader_pct, rel_tol=0, abs_tol=1e-6):
            errors.append(f"daily maximum does not match leader: {day}")
        signal = item.get("emotion_candle_signal")
        if not isinstance(signal, dict):
            errors.append(f"missing emotion candle signal: {day}")
        else:
            if signal.get("kind") != "height_anchored_previous_strong_pool_signal":
                errors.append(f"emotion candle kind mismatch: {day}")
            if signal.get("performance_date") != day:
                errors.append(f"emotion candle performance date mismatch: {day}")
            selection_date = signal.get("selection_date")
            if not selection_date or selection_date >= day:
                errors.append(f"emotion candle selection date invalid: {day}")
            base = signal.get("base_height_10d")
            offsets = signal.get("return_offsets") or {}
            display = signal.get("display") or {}
            if not isinstance(base, (int, float)) or not math.isclose(float(base), float(maximum) / 100.0, rel_tol=0, abs_tol=1e-6):
                errors.append(f"emotion candle base mismatch: {day}")
            for field in ("open", "high", "low", "close"):
                offset = offsets.get(field)
                shown = display.get(field)
                if not isinstance(offset, (int, float)) or not isinstance(shown, (int, float)):
                    errors.append(f"emotion candle {field} missing: {day}")
                elif isinstance(base, (int, float)) and not math.isclose(float(shown), float(base) + float(offset), rel_tol=0, abs_tol=1e-6):
                    errors.append(f"emotion candle {field} display mismatch: {day}")
            if all(isinstance(display.get(field), (int, float)) for field in ("open", "high", "low", "close")):
                if display["high"] + 1e-10 < max(display["open"], display["close"]):
                    errors.append(f"emotion candle high below body: {day}")
                if display["low"] - 1e-10 > min(display["open"], display["close"]):
                    errors.append(f"emotion candle low above body: {day}")
            requested = int(signal.get("requested_pool_size") or 0)
            selected = int(signal.get("selected_pool_size") or 0)
            valid = int(signal.get("valid_pool_size") or 0)
            members = signal.get("members") or []
            if requested <= 0 or selected <= 0 or selected > requested or valid <= 0 or valid > selected:
                errors.append(f"emotion candle pool size invalid: {day}")
            if len(members) != valid:
                errors.append(f"emotion candle member count mismatch: {day}")
            # 强池口径不变量：入池只数 = max(排行腿宽度, 达标只数)，达标股全收、不足 N 才用排行补
            rank_leg = int(signal.get("rank_leg_size") or 0)
            threshold_members = int(signal.get("threshold_members") or 0)
            target = max(rank_leg, threshold_members)
            if rank_leg <= 0 or threshold_members < 0 or threshold_members > selected:
                errors.append(f"emotion candle strong pool composition invalid: {day}")
            elif selected > target:
                errors.append(f"emotion candle strong pool exceeds threshold-union-rank target: {day}")
            elif selected < target:
                warnings.append(f"{day} 强池可选样本不足，入池 {selected}/{target}")
            if int(signal.get("rank_fill_members") or 0) != selected - threshold_members:
                errors.append(f"emotion candle rank fill member count mismatch: {day}")
            if not isinstance(signal.get("pool_median_return_10d"), (int, float)):
                errors.append(f"emotion candle pool median return missing: {day}")
            for member in members:
                if member.get("selection_date") != selection_date:
                    errors.append(f"emotion candle member selection date mismatch: {day} {member.get('ts_code')}")
                if not isinstance(member.get("selection_return_10d"), (int, float)):
                    errors.append(f"emotion candle member selection return missing: {day} {member.get('ts_code')}")
            if valid < selected:
                warnings.append(f"{day} 昨日强势股次日表现有效样本 {valid}/{selected}")
        group_signals = item.get("market_group_emotion_candle_signals") or {}
        group_maxima = item.get("market_group_maxima", {})
        if set(group_signals) != set(MARKET_GROUPS):
            errors.append(f"market group emotion candle signals incomplete: {day}")
        for group, group_signal in group_signals.items():
            if group not in MARKET_GROUPS:
                errors.append(f"unknown market group emotion candle signal: {day} {group}")
                continue
            if group_signal.get("scope") != "market_group" or group_signal.get("market_group") != group:
                errors.append(f"market group emotion candle scope mismatch: {day} {group}")
            if group_signal.get("selection_date") != (signal or {}).get("selection_date") or group_signal.get("performance_date") != day:
                errors.append(f"market group emotion candle dates mismatch: {day} {group}")
            group_leader = group_maxima.get(group) or {}
            group_base = group_signal.get("base_height_10d")
            if not isinstance(group_base, (int, float)) or not math.isclose(
                float(group_base), float(group_leader.get("pct")) / 100.0, rel_tol=0, abs_tol=1e-6
            ):
                errors.append(f"market group emotion candle base mismatch: {day} {group}")
            group_offsets = group_signal.get("return_offsets") or {}
            group_display = group_signal.get("display") or {}
            for field in ("open", "high", "low", "close"):
                offset = group_offsets.get(field)
                shown = group_display.get(field)
                if not isinstance(offset, (int, float)) or not isinstance(shown, (int, float)):
                    errors.append(f"market group emotion candle {field} missing: {day} {group}")
                elif isinstance(group_base, (int, float)) and not math.isclose(
                    float(shown), float(group_base) + float(offset), rel_tol=0, abs_tol=1e-6
                ):
                    errors.append(f"market group emotion candle {field} display mismatch: {day} {group}")
            selected_group = int(group_signal.get("selected_pool_size") or 0)
            valid_group = int(group_signal.get("valid_pool_size") or 0)
            members_group = group_signal.get("members") or []
            if selected_group <= 0 or valid_group <= 0 or valid_group > selected_group:
                errors.append(f"market group emotion candle pool size invalid: {day} {group}")
            if len(members_group) != valid_group or any(member.get("market_group") != group for member in members_group):
                errors.append(f"market group emotion candle members mismatch: {day} {group}")
            group_rank_leg = int(group_signal.get("rank_leg_size") or 0)
            group_threshold = int(group_signal.get("threshold_members") or 0)
            if (
                group_rank_leg <= 0
                or group_threshold > selected_group
                or selected_group > max(group_rank_leg, group_threshold)
            ):
                errors.append(f"market group emotion candle strong pool composition invalid: {day} {group}")
            if valid_group < selected_group:
                warnings.append(f"{day} {group}昨日强势股次日表现有效样本 {valid_group}/{selected_group}")
        change = item.get("maximum_change_vs_previous")
        if previous_maximum is None:
            if change is not None:
                errors.append(f"first daily maximum change must be null: {day}")
        elif change is None or not math.isclose(float(change), float(maximum) - previous_maximum, rel_tol=0, abs_tol=1e-6):
            errors.append(f"daily maximum change mismatch: {day}")
        market_maxima = item.get("market_maxima", {})
        for market, market_leader in market_maxima.items():
            if market not in ALLOWED_MARKETS:
                errors.append(f"disallowed market maximum: {day} {market}")
            if market_leader.get("market") != market:
                errors.append(f"market maximum label mismatch: {day} {market}")
            if float(market_leader.get("pct")) > float(maximum) + 1e-10:
                errors.append(f"market maximum exceeds overall maximum: {day} {market}")
        group_maxima = item.get("market_group_maxima", {})
        if set(group_maxima) != set(MARKET_GROUPS):
            errors.append(f"market group maxima incomplete: {day}")
        for group, group_leader in group_maxima.items():
            if group_leader.get("market") not in MARKET_GROUPS.get(group, set()):
                errors.append(f"market group maximum mismatch: {day} {group}")
            if group_leader.get("market_group") != group:
                errors.append(f"market group label mismatch: {day} {group}")
            if float(group_leader.get("pct")) > float(maximum) + 1e-10:
                errors.append(f"market group maximum exceeds overall maximum: {day} {group}")
        group_peak = max(float(row.get("pct")) for row in group_maxima.values()) if group_maxima else None
        if group_peak is None or not math.isclose(group_peak, float(maximum), rel_tol=0, abs_tol=1e-6):
            errors.append(f"overall maximum does not equal group maximum: {day}")
        previous_maximum = float(maximum)
    failed = int(data.get("source", {}).get("bootstrap_failed_symbols", 0) or 0)
    failed_risk_only = int(data.get("source", {}).get("bootstrap_failed_risk_only", 0) or 0)
    effective_failed = int(data.get("source", {}).get("bootstrap_effective_failed_symbols", failed - failed_risk_only) or 0)
    if effective_failed:
        errors.append(f"bootstrap has {effective_failed} failed potentially qualified symbols")
    elif failed_risk_only:
        warnings.append(f"bootstrap failed for {failed_risk_only} current risk-name symbols; they are excluded by rule")
    warning = str(data.get("warning", ""))
    if "ST" in warning:
        warnings.append(warning)
    result = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "dates": len(dates),
        "qualified_rows": len(rows),
        "unique_stocks": len({row.get("ts_code") for row in rows}),
        "excluded_threshold_hits": len(excluded),
        "daily_maxima": len([item for item in daily if item.get("maximum_return_10d") is not None]),
        "market_group_maxima": sum(len(item.get("market_group_maxima", {})) for item in daily),
    }
    data["validation"] = result
    for item in data.get("process_log", []):
        if item.get("component") == "校验与报告脚本":
            item["status"] = "完成" if result["valid"] else "校验失败"
    args.input.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
