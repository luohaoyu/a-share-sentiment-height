#!/usr/bin/env python3
"""Validate the daily limit-up ladder payload before it reaches the report.

Hard invariants (each one is a real failure mode, not decoration):
    1. ``dates`` ascending, unique, and equal in length to ``points``
    2. ``by_boards`` values sum to ``total``
    3. ``total == first_board + consecutive``
    4. ``max_boards == max(by_boards keys)`` (0 when the day has no limit-up)
    5. every ``ladder`` row has ``boards >= scope.ladder_min_boards`` and the
       per-board counts never exceed ``by_boards``
    6. counts are non-negative integers, ``consecutive_ratio`` in [0, 1]
    7. the first session must be flagged as a lower bound (no predecessor data)

Exit code 0 = pass (warnings allowed), 1 = fail.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def main() -> int:
    parser = argparse.ArgumentParser(description="校验每日涨停连板梯队数据")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--expected-dates", type=int, default=None, help="期望的交易日数")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"错误：无法读取 {args.input}: {exc}", file=sys.stderr)
        return 1

    dates = data.get("dates")
    points = data.get("points")
    window = data.get("window") or {}
    scope = data.get("scope") or {}

    if not isinstance(dates, list) or not dates:
        errors.append("dates 缺失或为空")
        dates = []
    if not isinstance(points, list) or not points:
        errors.append("points 缺失或为空")
        points = []
    if len(dates) != len(points):
        errors.append(f"points 长度 {len(points)} 与 dates 长度 {len(dates)} 不一致")
    if dates != sorted(dates):
        errors.append("dates 不是升序")
    if len(set(dates)) != len(dates):
        errors.append("dates 存在重复交易日")
    if args.expected_dates is not None and len(dates) != args.expected_dates:
        errors.append(f"交易日数 {len(dates)} 不等于期望 {args.expected_dates}")

    if window.get("lower_bound_before") != (dates[0] if dates else None):
        errors.append(
            "window.lower_bound_before 必须等于首个交易日，否则连板下限无法被下游识别"
        )
    if not window.get("lower_bound_note"):
        warnings.append("window.lower_bound_note 为空，报告将无法解释连板下限")

    min_boards = scope.get("ladder_min_boards")
    if not isinstance(min_boards, int) or min_boards < 1:
        warnings.append("scope.ladder_min_boards 缺失或非法，按 2 处理")
        min_boards = 2

    for index, point in enumerate(points):
        label = point.get("date") or f"points[{index}]"
        if point.get("date") != (dates[index] if index < len(dates) else None):
            errors.append(f"{label}: point.date 与 dates[{index}] 不一致")

        total = point.get("total")
        first_board = point.get("first_board")
        consecutive = point.get("consecutive")
        max_boards = point.get("max_boards")
        by_boards = point.get("by_boards")

        for key, value in (
            ("total", total),
            ("first_board", first_board),
            ("consecutive", consecutive),
            ("max_boards", max_boards),
        ):
            if not is_count(value):
                errors.append(f"{label}: {key}={value!r} 不是非负整数")
        if not isinstance(by_boards, dict):
            errors.append(f"{label}: by_boards 缺失或不是对象")
            continue

        parsed: dict[int, int] = {}
        for raw_key, raw_value in by_boards.items():
            try:
                boards = int(raw_key)
            except (TypeError, ValueError):
                errors.append(f"{label}: by_boards 键 {raw_key!r} 不是整数板数")
                continue
            if boards < 1:
                errors.append(f"{label}: by_boards 键 {boards} 小于 1")
            if not is_count(raw_value):
                errors.append(f"{label}: by_boards[{raw_key}]={raw_value!r} 不是非负整数")
                continue
            parsed[boards] = raw_value

        board_sum = sum(parsed.values())
        if is_count(total) and board_sum != total:
            errors.append(f"{label}: by_boards 求和 {board_sum} 不等于 total {total}")
        if is_count(total) and is_count(first_board) and is_count(consecutive):
            if first_board + consecutive != total:
                errors.append(
                    f"{label}: first_board({first_board}) + consecutive({consecutive}) != total({total})"
                )
            if parsed.get(1, 0) != first_board:
                errors.append(
                    f"{label}: by_boards['1']={parsed.get(1, 0)} 不等于 first_board={first_board}"
                )
        expected_max = max(parsed) if parsed else 0
        if is_count(max_boards) and max_boards != expected_max:
            errors.append(f"{label}: max_boards={max_boards} 与梯队最高板 {expected_max} 不一致")

        ratio = point.get("consecutive_ratio")
        if ratio is not None:
            if not isinstance(ratio, (int, float)) or not (0 <= ratio <= 1):
                errors.append(f"{label}: consecutive_ratio={ratio!r} 不在 [0,1]")

        ladder = point.get("ladder")
        if not isinstance(ladder, list):
            errors.append(f"{label}: ladder 缺失或不是数组")
            continue
        if point.get("ladder_truncated") and not ladder:
            warnings.append(f"{label}: ladder_truncated=true 但 ladder 为空")
        ladder_counts: dict[int, int] = {}
        for row in ladder:
            boards = row.get("boards")
            if not isinstance(boards, int) or boards < min_boards:
                errors.append(f"{label}: ladder 行 {row.get('ts_code')} 板数 {boards!r} 低于 {min_boards}")
                continue
            ladder_counts[boards] = ladder_counts.get(boards, 0) + 1
            if not row.get("ts_code") or not row.get("name"):
                errors.append(f"{label}: ladder 行缺少 ts_code 或 name")
        if not point.get("ladder_truncated"):
            for boards, count in ladder_counts.items():
                if count > parsed.get(boards, 0):
                    errors.append(
                        f"{label}: ladder 中 {boards} 板 {count} 只，超过 by_boards 的 {parsed.get(boards, 0)} 只"
                    )

        unknown = point.get("unknown_rows")
        if not is_count(unknown):
            warnings.append(f"{label}: unknown_rows 缺失或非法")

    for key in ("total_change", "max_boards_change"):
        first = points[0].get(key) if points else None
        if first is not None:
            warnings.append(f"首个交易日的 {key} 应为 null（无前一日可比），当前为 {first}")

    audit = data.get("audit") or {}
    if not audit.get("symbols_in_scope"):
        warnings.append("audit.symbols_in_scope 缺失，无法核对统计范围")
    if audit.get("excluded_st_limit_up_hits"):
        warnings.append(
            f"有 {audit['excluded_st_limit_up_hits']} 次 ST 涨停因口径排除未计入（scope.exclude_st=true）"
        )

    result = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked": {"dates": len(dates), "points": len(points)},
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
