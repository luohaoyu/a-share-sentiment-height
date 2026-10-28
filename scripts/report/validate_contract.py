"""按 report.contract.json 校验 data.json。

零依赖：不引 jsonschema，只用标准库做结构性校验。

重点防线（都来自真实踩过的坑）：
    1. contract_version 必须匹配。
    2. 曲线 points 长度必须与 x 等长，否则前端错位。
    3. y_max 必须 >= 所有数据点，否则折线被裁顶。
    4. status != confirmed 时终点必须为 null（禁止伪造终点）。
    5. gain 类字段若绝对值 > GAIN_SUSPECT，高度怀疑数据侧误乘了 100。

用法：
    python scripts/validate_contract.py --data data.json
    python scripts/validate_contract.py --data data.json --contract assets/report.contract.json
默认契约固定在 <skill>/assets/report.contract.json，无需显式传入。
退出码：0 通过（可能有 warning），1 失败。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

GAIN_SUSPECT = 10.0  # |gain| > 1000% 视为可疑，多半是百分数没除以 100
STATUSES = {"confirmed", "pending", "data_insufficient", "rejected"}

_HERE = Path(__file__).resolve().parent
_SKILL_ROOT = next(
    (p for p in (_HERE, *_HERE.parents) if (p / "SKILL.md").exists()),
    _HERE.parents[1],
)
DEFAULT_CONTRACT = _SKILL_ROOT / "assets" / "report.contract.json"

GAIN_FIELDS = {
    "structure_waves": ["gain_theoretical", "gain_observable", "gain_dual"],
    "emotion_rallies": ["gain"],
}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": not self.errors,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def check_meta(data: dict[str, Any], contract: dict[str, Any], report: Report) -> None:
    meta = data.get("meta")
    if not isinstance(meta, dict):
        report.error("meta 缺失或不是对象")
        return
    spec = contract["schema"]["meta"]["required"]
    for key in spec:
        if meta.get(key) is None:
            report.error(f"meta.{key} 必填但为 null")
    if meta.get("return_days") is not None:
        try:
            if int(meta["return_days"]) <= 0:
                report.error(f"meta.return_days={meta['return_days']} 必须为正整数")
        except (TypeError, ValueError):
            report.error(f"meta.return_days={meta['return_days']} 必须为整数")
    threshold = meta.get("threshold")
    if isinstance(threshold, (int, float)) and not (0 < threshold < 1):
        report.error(f"meta.threshold={threshold} 应为小数比例（如 0.5），当前疑似百分数")


def check_pool_trends(data: dict[str, Any], report: Report) -> None:
    pool = data.get("pool_trends")
    if not isinstance(pool, dict):
        report.error("pool_trends 缺失")
        return
    x = pool.get("x") or []
    series = pool.get("series") or []
    if not series:
        report.error("pool_trends.series 为空")
        return
    y_max = pool.get("y_max")
    if not isinstance(y_max, (int, float)):
        report.error("pool_trends.y_max 缺失或非数值")
        y_max = None

    finite: list[float] = []
    for item in series:
        key = item.get("key")
        points = item.get("points") or []
        if len(points) != len(x):
            report.error(
                f"series[{key}] points 长度 {len(points)} 与 x 长度 {len(x)} 不一致（前端会错位）"
            )
        for index, value in enumerate(points):
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                report.error(f"series[{key}].points[{index}] 非数值：{value!r}")
                continue
            if abs(value) > GAIN_SUSPECT:
                report.error(
                    f"series[{key}].points[{index}]={value} 超过 {GAIN_SUSPECT}，"
                    "疑似数据侧误乘 100（契约要求小数比例）"
                )
            finite.append(value)
    if y_max is not None and finite and max(finite) > y_max:
        report.error(f"y_max={y_max} 小于实际最大值 {max(finite):.4f}，折线会被裁顶")

    def check_max_series(scope: dict[str, Any], label: str) -> None:
        max_series = scope.get("max_series") or {}
        points = max_series.get("points") or []
        if len(points) != len(x):
            report.error(f"{label}.max_series.points 长度 {len(points)} 与 x 长度 {len(x)} 不一致")
        metric_points = {item.get("key"): item.get("points") or [] for item in (scope.get("series") or [])}
        for index, point in enumerate(points):
            available = [values[index] for values in metric_points.values() if index < len(values) and values[index] is not None]
            expected = max(available) if available else None
            if (point is None) != (expected is None) or (point is not None and abs(float(point) - float(expected)) > 1e-6):
                report.error(f"{label}.max_series.points[{index}] 与 T1/T2/T3 最大值不一致")
            winners = set((max_series.get("winning_metrics") or [])[index] if index < len(max_series.get("winning_metrics") or []) else [])
            expected_winners = {key for key, values in metric_points.items() if index < len(values) and values[index] is not None and expected is not None and abs(float(values[index]) - float(expected)) <= 1e-6}
            if winners != expected_winners:
                report.error(f"{label}.max_series.winning_metrics[{index}] 与最大口径不一致")
    check_max_series(pool, "pool_trends")
    for market, scope in (pool.get("by_market") or {}).items():
        check_max_series(scope, "pool_trends.by_market[" + str(market) + "]")

    coverage = pool.get("coverage") or {}
    total = coverage.get("pool_union") or 0
    # 缺失分母只取阈值达标成员：补位股（rank_fill）本就未纳入三口径计算，
    # 计入缺口会把「未计算」误报成「数据缺失」。
    threshold_total = coverage.get("threshold_members")
    if threshold_total is None:
        threshold_total = total
    fill_total = coverage.get("rank_fill_members") or 0
    gap = (coverage.get("no_structure_start") or 0) + (coverage.get("no_first_board") or 0)
    if threshold_total and gap:
        suffix = f"；另有前10名补位 {fill_total} 只未纳入波段计算" if fill_total else ""
        breakdown = coverage.get("no_first_board_reason_counts") or {}
        category_counts = coverage.get("no_first_board_category_counts") or {}
        structural = category_counts.get("structural") or 0
        detail = ""
        if breakdown:
            detail = "（成因：" + "、".join(f"{k} {v} 只" for k, v in breakdown.items()) + "）"
        if sum(breakdown.values()) != int(coverage.get("no_first_board") or 0):
            report.error("pool_trends.coverage.no_first_board_reason_counts 合计与 no_first_board 不一致")
        if sum(category_counts.values()) != int(coverage.get("no_first_board") or 0):
            report.error("pool_trends.coverage.no_first_board_category_counts 合计与 no_first_board 不一致")
        nature = (
            f"其中 {structural} 只属于结构性无首板（趋势型无涨停 / 首板早于搜索窗口），并非数据缺失"
            if structural
            else "均为数据缺失或不足"
        )
        report.warn(
            f"覆盖缺口：达标成员 {threshold_total} 只，其中无底分型起点 {coverage.get('no_structure_start')} 只、"
            f"无首板日 {coverage.get('no_first_board')} 只{detail}；{nature}，T3 曲线样本范围小于 T1{suffix}"
        )


def check_daily_snapshots(data: dict[str, Any], report: Report) -> None:
    pool = data.get("pool_trends") or {}
    dates = pool.get("x") or []
    snapshots = pool.get("daily_snapshots") or []
    if len(snapshots) != len(dates):
        report.error(f"pool_trends.daily_snapshots 长度 {len(snapshots)} 与 x 长度 {len(dates)} 不一致")
    for index, snapshot in enumerate(snapshots):
        day = snapshot.get("date")
        if index < len(dates) and day != dates[index]:
            report.error(f"daily_snapshots[{index}].date={day!r} 与 x[{index}]={dates[index]!r} 不一致")
        members = snapshot.get("members") or []
        if snapshot.get("count") != len(members):
            report.error(f"daily_snapshots[{day}].count 与 members 长度不一致")
        ranks = [row.get("rank") for row in members]
        if ranks != list(range(1, len(members) + 1)):
            report.error(f"daily_snapshots[{day}] rank 必须从 1 连续递增")
        if len({str(row.get("ts_code") or row.get("code")) for row in members}) != len(members):
            report.error(f"daily_snapshots[{day}] 存在重复股票")
        for row in members:
            if row.get("pool_change") not in {"baseline", "new", "continued"}:
                report.error(f"daily_snapshots[{day}] pool_change 非法：{row.get('pool_change')!r}")
            if bool(row.get("is_new_entry")) != (row.get("pool_change") == "new"):
                report.error(f"daily_snapshots[{day}] is_new_entry 与 pool_change 不一致")
        compare_date = snapshot.get("exit_compare_date")
        exits = snapshot.get("exit_members") or []
        compare_dates = snapshot.get("exit_compare_dates") or []
        if compare_date is None and exits:
            report.error(f"daily_snapshots[{day}] 无前5个交易日比较日期但存在退出个股")
        if compare_dates and compare_date != compare_dates[-1]:
            report.error(f"daily_snapshots[{day}] exit_compare_date 应为前5日比较窗口最后日期")
        if snapshot.get("exit_compare_start_date") != (compare_dates[0] if compare_dates else None):
            report.error(f"daily_snapshots[{day}] exit_compare_start_date 与比较窗口不一致")
        if snapshot.get("exit_compare_end_date") != (compare_dates[-1] if compare_dates else None):
            report.error(f"daily_snapshots[{day}] exit_compare_end_date 与比较窗口不一致")
        if snapshot.get("exit_count") != len(exits):
            report.error(f"daily_snapshots[{day}] exit_count 与 exit_members 长度不一致")
        exit_codes = {str(item.get("ts_code") or item.get("code")) for item in exits}
        if len(exit_codes) != len(exits):
            report.error(f"daily_snapshots[{day}] 退出个股存在重复股票")
        stats = snapshot.get("stats") or {}
        scopes = {"all": members}
        by_market = {}
        for row in members:
            group = row.get("market_group")
            if group:
                by_market.setdefault(group, []).append(row)
        scopes.update({"by_market:" + key: rows for key, rows in by_market.items()})
        for scope_name, scope_members in scopes.items():
            scope_stats = stats.get("all" if scope_name == "all" else "by_market", {})
            if scope_name != "all":
                scope_stats = scope_stats.get(scope_name.split(":", 1)[1], {})
            metrics = scope_stats.get("metrics") or {}
            for key in ("t1", "t2", "t3"):
                metric = metrics.get(key) or {}
                values = []
                unavailable = 0
                for row in scope_members:
                    item = row.get(key) or {}
                    status = item.get("status")
                    if status == "available":
                        if not isinstance(item.get("gain"), (int, float)):
                            report.error(f"daily_snapshots[{day}] {scope_name}.{key} available 但 gain 非数值")
                        else:
                            values.append(float(item["gain"]))
                        if any(item.get(name) is not None for name in ("reason_code", "reason_category", "reason")):
                            report.error(f"daily_snapshots[{day}] {scope_name}.{key} available 但含异常原因")
                    elif status == "unavailable":
                        unavailable += 1
                        if item.get("gain") is not None or not item.get("reason_code") or not item.get("reason_category") or not item.get("reason"):
                            report.error(f"daily_snapshots[{day}] {scope_name}.{key} unavailable 原因或 gain 不完整")
                    else:
                        report.error(f"daily_snapshots[{day}] {scope_name}.{key} status 非法：{status!r}")
                if metric.get("available") != len(values) or metric.get("unavailable") != unavailable:
                    report.error(f"daily_snapshots[{day}] {scope_name}.{key} 可用/不可用统计不一致")
                reason_counts = metric.get("reason_counts") or {}
                category_counts = metric.get("reason_category_counts") or {}
                if sum(reason_counts.values()) != unavailable or sum(category_counts.values()) != unavailable:
                    report.error(f"daily_snapshots[{day}] {scope_name}.{key} 异常统计合计不一致")
                maxima = metric.get("maxima") or {}
                expected = max(values) if values else None
                actual = maxima.get("gain")
                if (actual is None) != (expected is None) or (actual is not None and abs(float(actual) - expected) > 1e-6):
                    report.error(f"daily_snapshots[{day}] {scope_name}.{key} maxima 与成员最大值不一致")
                leader_codes = {str(x.get("ts_code") or x.get("code")) for x in maxima.get("leaders") or []}
                expected_codes = {
                    str(row.get("ts_code") or row.get("code")) for row in scope_members
                    if (row.get(key) or {}).get("gain") is not None and expected is not None
                    and abs(float((row.get(key) or {}).get("gain")) - expected) <= 1e-6
                }
                if leader_codes != expected_codes:
                    report.error(f"daily_snapshots[{day}] {scope_name}.{key} leaders 与最大值成员不一致")


def check_window_stats(data: dict[str, Any], report: Report) -> None:
    pool = data.get("pool_trends") or {}
    dates = pool.get("x") or []
    snapshots = pool.get("daily_snapshots") or []
    window = pool.get("window_stats") or {}
    if window.get("date_start") != (dates[0] if dates else None) or window.get("date_end") != (dates[-1] if dates else None):
        report.error("pool_trends.window_stats 日期边界与 x 不一致")
    def check_scope(scope: dict[str, Any], label: str, expected_rows: list[dict[str, Any]]) -> None:
        if scope.get("snapshot_count") != len(dates):
            report.error(f"{label}.snapshot_count 与窗口长度不一致")
        for key in ("t1", "t2", "t3"):
            metric = (scope.get("metrics") or {}).get(key) or {}
            target_codes = {str(row.get("ts_code") or row.get("code")) for row in expected_rows}
            unavailable_events = sum(1 for snapshot in snapshots for row in (snapshot.get("members") or [])
                                    if str(row.get("ts_code") or row.get("code")) in target_codes and (row.get(key) or {}).get("status") == "unavailable")
            if metric.get("unavailable_events") != unavailable_events:
                report.error(f"{label}.{key}.unavailable_events 不一致")
            items = metric.get("items") or []
            for item in items:
                item_dates = item.get("dates") or []
                if item.get("occurrence_count") != len(item_dates) or item_dates != sorted(set(item_dates)):
                    report.error(f"{label}.{key} 异常个股明细日期不一致：{item.get('code')}")
                if item_dates and (item.get("first_date") != item_dates[0] or item.get("last_date") != item_dates[-1]):
                    report.error(f"{label}.{key} 异常个股首末日期不一致：{item.get('code')}")
                if any(day not in dates for day in item_dates):
                    report.error(f"{label}.{key} 异常个股存在窗口外日期：{item.get('code')}")
            if metric.get("affected_stock_count") != len(items):
                report.error(f"{label}.{key}.affected_stock_count 与 items 不一致")
    all_rows = [row for snapshot in snapshots for row in (snapshot.get("members") or [])]
    check_scope(window.get("all") or {}, "window_stats.all", all_rows)
    for group, scope in (window.get("by_market") or {}).items():
        rows = [row for row in all_rows if row.get("market_group") == group]
        check_scope(scope, f"window_stats.by_market.{group}", rows)


def check_daily_leaders(data: dict[str, Any], report: Report) -> None:
    pool = data.get("pool_trends") or {}
    snapshots = pool.get("daily_snapshots") or []
    leaders = pool.get("daily_leaders") or []
    if len(leaders) != len(snapshots):
        report.error("pool_trends.daily_leaders 长度与 daily_snapshots 不一致")
    for index, row in enumerate(leaders):
        if index >= len(snapshots):
            break
        if row.get("date") != snapshots[index].get("date"):
            report.error(f"daily_leaders[{index}].date 与快照不一致")
        stats = snapshots[index].get("stats") or {}
        scopes = {"all": row.get("all") or {}}
        scopes.update(row.get("by_market") or {})
        for scope_name, scope in scopes.items():
            original = (stats.get("all") if scope_name == "all" else (stats.get("by_market") or {}).get(scope_name)) or {}
            for key in ("t1", "t2", "t3"):
                expected = (((original.get("metrics") or {}).get(key) or {}).get("maxima") or {})
                actual = (scope.get(key) or {})
                if actual.get("gain") != expected.get("gain"):
                    report.error(f"daily_leaders[{row.get('date')}].{scope_name}.{key}.gain 与快照不一致")
                expected_codes = {str(item.get("ts_code") or item.get("code")) for item in expected.get("leaders") or []}
                actual_codes = {str(item.get("ts_code") or item.get("code")) for item in actual.get("leaders") or []}
                if expected_codes != actual_codes:
                    report.error(f"daily_leaders[{row.get('date')}].{scope_name}.{key}.leaders 与快照不一致")


def check_market_timeline(data: dict[str, Any], report: Report) -> None:
    dates = data.get("dates") or []
    timeline = data.get("market_timeline") or {}
    points = timeline.get("points") or []
    if len(points) != len(dates):
        report.error("market_timeline.points 长度与 dates 不一致")
    for index, point in enumerate(points):
        if index < len(dates) and point.get("date") != dates[index]:
            report.error(f"market_timeline[{index}].date 与 dates 不一致")
        tags = point.get("tags") or []
        if bool(point.get("is_divergence")) != bool(tags):
            report.error(f"market_timeline[{index}] is_divergence 与 tags 不一致")
        if point.get("score") != len(tags):
            report.error(f"market_timeline[{index}].score 与 tags 数量不一致")


def check_rows(data: dict[str, Any], report: Report) -> None:
    for section, gain_fields in GAIN_FIELDS.items():
        rows = data.get(section)
        if rows is None:
            report.error(f"{section} 缺失")
            continue
        if not isinstance(rows, list):
            report.error(f"{section} 不是数组")
            continue
        for index, row in enumerate(rows):
            name = row.get("name") or row.get("code") or f"#{index}"
            status = row.get("status")
            if status not in STATUSES:
                report.error(f"{section}[{name}] status={status!r} 不在枚举内")
            for field in gain_fields:
                value = row.get(field)
                if value is None:
                    continue
                if not isinstance(value, (int, float)):
                    report.error(f"{section}[{name}].{field} 非数值：{value!r}")
                elif abs(value) > GAIN_SUSPECT:
                    report.error(
                        f"{section}[{name}].{field}={value} 超过 {GAIN_SUSPECT}，"
                        "疑似百分数未除以 100"
                    )
            end_field = "end_date" if section == "structure_waves" else "trend_end_date"
            if status == "pending" and row.get(end_field) is not None:
                report.error(f"{section}[{name}] status=pending 但 {end_field}={row.get(end_field)}，禁止伪造终点")


def check_height(data: dict[str, Any], report: Report) -> None:
    height = data.get("height")
    if not isinstance(height, dict):
        report.error("height 缺失")
        return
    points = height.get("points") or []
    dates = data.get("dates") or []
    if dates and len(points) != len(dates):
        report.error(f"height.points 长度 {len(points)} 与 dates 长度 {len(dates)} 不一致")
    groups = height.get("market_groups") or []
    empty_days = sum(1 for p in points if p.get("value") is None)
    if points and empty_days == len(points):
        report.warn("height 全窗口无达标交易日")
    for index, point in enumerate(points):
        if index < 5:
            for group in groups:
                if group not in (point.get("by_market") or {}):
                    report.error(f"height.points[{index}].by_market 缺少分组 {group}")
                    break
        signal = point.get("candle_signal")
        if signal is None:
            continue
        if signal.get("kind") != "height_anchored_previous_strong_pool_signal":
            report.error(f"height.points[{index}].candle_signal.kind 非法")
        selection_date = signal.get("selection_date")
        performance_date = signal.get("performance_date")
        if performance_date != point.get("date"):
            report.error(f"height.points[{index}].candle_signal.performance_date 与点日期不一致")
        if not selection_date or selection_date >= performance_date:
            report.error(f"height.points[{index}].candle_signal.selection_date 非昨日交易日")
        selected = signal.get("selected_pool_size")
        valid = signal.get("valid_pool_size")
        requested = signal.get("requested_pool_size")
        if not all(isinstance(v, int) for v in (selected, valid, requested)) or not (0 < valid <= selected <= requested):
            report.error(f"height.points[{index}].candle_signal 样本数非法")
        base = signal.get("base_height_10d")
        if point.get("value") is not None and isinstance(base, (int, float)) and abs(float(point["value"]) - float(base)) > 1e-6:
            report.error(f"height.points[{index}] K线基准与10日涨幅最大值不一致")
        offsets = signal.get("return_offsets") or {}
        display = signal.get("display") or {}
        for field in ("open", "high", "low", "close"):
            offset = offsets.get(field)
            shown = display.get(field)
            if not isinstance(offset, (int, float)) or not isinstance(shown, (int, float)):
                report.error(f"height.points[{index}].candle_signal.{field} 缺失或非数值")
                continue
            if isinstance(base, (int, float)) and abs(float(shown) - float(base) - float(offset)) > 1e-6:
                report.error(f"height.points[{index}].candle_signal.display.{field} 未按基准+偏移生成")
        if all(isinstance(display.get(k), (int, float)) for k in ("open", "high", "low", "close")):
            if display["high"] + 1e-9 < max(display["open"], display["close"]):
                report.error(f"height.points[{index}] K线 high 低于实体")
            if display["low"] - 1e-9 > min(display["open"], display["close"]):
                report.error(f"height.points[{index}] K线 low 高于实体")
        market_signals = point.get("market_candle_signals") or {}
        for group in groups:
            group_signal = market_signals.get(group)
            group_value = (point.get("by_market") or {}).get(group)
            if group_value is None:
                continue
            if not isinstance(group_signal, dict):
                report.error(f"height.points[{index}].market_candle_signals 缺少 {group}")
                continue
            if group_signal.get("scope") != "market_group" or group_signal.get("market_group") != group:
                report.error(f"height.points[{index}].market_candle_signals[{group}] 范围非法")
            if group_signal.get("performance_date") != point.get("date") or group_signal.get("selection_date") != selection_date:
                report.error(f"height.points[{index}].market_candle_signals[{group}] 日期非法")
            group_base = group_signal.get("base_height_10d")
            if not isinstance(group_base, (int, float)) or abs(float(group_value) - float(group_base)) > 1e-6:
                report.error(f"height.points[{index}].market_candle_signals[{group}] 基准与分市场最大值不一致")
            group_offsets = group_signal.get("return_offsets") or {}
            group_display = group_signal.get("display") or {}
            for field in ("open", "high", "low", "close"):
                offset = group_offsets.get(field)
                shown = group_display.get(field)
                if not isinstance(offset, (int, float)) or not isinstance(shown, (int, float)):
                    report.error(f"height.points[{index}].market_candle_signals[{group}].{field} 缺失")
                elif isinstance(group_base, (int, float)) and abs(float(shown) - float(group_base) - float(offset)) > 1e-6:
                    report.error(f"height.points[{index}].market_candle_signals[{group}].display.{field} 非基准+偏移")
            members = group_signal.get("members") or []
            if any(member.get("market_group") != group for member in members):
                report.error(f"height.points[{index}].market_candle_signals[{group}] 混入其他市场成员")


def check_details(data: dict[str, Any], report: Report) -> None:
    details = data.get("details")
    if not isinstance(details, dict):
        report.error("details 缺失（懒加载层）")
        return
    codes = set()
    for section in ("structure_waves", "emotion_rallies"):
        for row in data.get(section) or []:
            key = row.get("ts_code") or row.get("code")
            if key:
                codes.add(str(key))
    missing = codes - set(details.keys())
    if missing:
        report.warn(f"{len(missing)} 只股票无明细：{', '.join(sorted(missing)[:5])}")


def check_limit_up(data: dict[str, Any], report: Report) -> None:
    """涨停连板梯队：计数一致性是硬约束，缺失只降级不报错。

    ``limit_up`` 为 None 是合法降级（旧缓存没有该层），此时只 warn；
    一旦给了对象，就必须自洽——前端会直接按 by_boards 画堆叠柱，
    求和不等会在页面上表现为「柱子比总数高」这种明显的错。
    """
    limit_up = data.get("limit_up")
    if limit_up is None:
        report.warn("limit_up 缺失（本次未产出涨停连板指标），SPA 将隐藏该视图")
        return
    if not isinstance(limit_up, dict):
        report.error("limit_up 不是对象")
        return
    if limit_up.get("unit") != "count":
        report.error(f"limit_up.unit={limit_up.get('unit')!r} 必须为 'count'（计数不是比例）")

    points = limit_up.get("points") or []
    dates = data.get("dates") or []
    if dates and len(points) != len(dates):
        report.error(f"limit_up.points 长度 {len(points)} 与 dates 长度 {len(dates)} 不一致")
    if not points:
        report.error("limit_up.points 为空")
        return

    groups = limit_up.get("market_groups") or []
    min_boards = int((limit_up.get("scope") or {}).get("ladder_min_boards") or 2)
    with_data = 0
    for index, point in enumerate(points):
        label = point.get("date") or f"points[{index}]"
        total = point.get("total")
        if total is None:
            # 缺失日：其余计数必须一起为 null，否则前端会出现「总数空但梯队有值」
            for key in ("first_board", "consecutive", "max_boards", "consecutive_ratio"):
                if point.get(key) is not None:
                    report.error(f"{label}: total 为 null 但 {key}={point.get(key)!r}，缺失日必须整体为空")
            continue
        with_data += 1
        if not isinstance(total, int) or total < 0:
            report.error(f"{label}: total={total!r} 不是非负整数")
            continue
        by_boards = point.get("by_boards") or {}
        if not isinstance(by_boards, dict):
            report.error(f"{label}: by_boards 不是对象")
            by_boards = {}
        parsed: dict[int, int] = {}
        for raw_key, raw_value in by_boards.items():
            try:
                parsed[int(raw_key)] = int(raw_value)
            except (TypeError, ValueError):
                report.error(f"{label}: by_boards 条目 {raw_key!r}={raw_value!r} 不是整数")
                continue
        board_sum = sum(parsed.values())
        if board_sum != total:
            report.error(f"{label}: by_boards 求和 {board_sum} 与 total {total} 不一致")
        first_board = point.get("first_board")
        consecutive = point.get("consecutive")
        if isinstance(first_board, int) and isinstance(consecutive, int):
            if first_board + consecutive != total:
                report.error(
                    f"{label}: first_board({first_board}) + consecutive({consecutive}) != total({total})"
                )
        expected_max = max(parsed) if parsed else 0
        if point.get("max_boards") != expected_max:
            report.error(f"{label}: max_boards={point.get('max_boards')!r} 与梯队最高板 {expected_max} 不一致")
        ratio = point.get("consecutive_ratio")
        if ratio is not None and (not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1):
            report.error(f"{label}: consecutive_ratio={ratio!r} 不在 [0,1]（应为小数比例）")

        by_market = point.get("by_market") or {}
        if groups:
            missing_groups = [g for g in groups if g not in by_market]
            if missing_groups:
                report.error(f"{label}: by_market 缺少分组 {missing_groups}")
            elif sum(v for v in by_market.values() if isinstance(v, int)) != total:
                report.warn(f"{label}: by_market 分组合计与 total 不等，需核对市场归类")
        ladder = point.get("ladder") or []
        for row in ladder:
            boards = row.get("boards")
            if not isinstance(boards, int) or boards < min_boards:
                report.error(f"{label}: ladder 行板数 {boards!r} 低于最低板数 {min_boards}")

    if with_data == 0:
        report.error("limit_up 全窗口无有效数据")
    elif with_data < 3:
        report.warn(f"limit_up 仅 {with_data} 个交易日有数据，不足以判断连板结构")


def main() -> int:
    parser = argparse.ArgumentParser(description="按契约校验 data.json")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    if not args.contract.exists():
        print(f"失败：契约文件不存在 {args.contract}")
        return 1
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    data = json.loads(args.data.read_text(encoding="utf-8"))
    report = Report()

    expected = contract.get("contract_version")
    actual = data.get("contract_version")
    if actual != expected:
        report.error(f"contract_version 不匹配：data={actual!r} contract={expected!r}")
    else:
        check_meta(data, contract, report)
        check_height(data, report)
        check_limit_up(data, report)
        check_pool_trends(data, report)
        check_daily_snapshots(data, report)
        check_window_stats(data, report)
        check_daily_leaders(data, report)
        check_market_timeline(data, report)
        check_rows(data, report)
        check_details(data, report)

    result = report.to_dict()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(("通过" if result["valid"] else "失败") + f"：{len(result['errors'])} 个错误，"
              f"{len(result['warnings'])} 个警告")
        for item in result["errors"]:
            print("  [ERROR]", item)
        for item in result["warnings"]:
            print("  [WARN ]", item)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
