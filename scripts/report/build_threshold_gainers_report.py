#!/usr/bin/env python3
"""Render an emotion-height report led by each session's maximum 10-day return."""
from __future__ import annotations

import argparse
import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

MARKET_GROUP_ORDER = ["沪深主板", "科创成长板"]
MARKET_GROUP_SOURCES = {
    "沪深主板": ["沪市主板", "深市主板"],
    "科创成长板": ["科创板", "创业板"],
}
SERIES_COLORS = {
    "总体极值": "#111827",
    "沪深主板": "#dc2626",
    "科创成长板": "#7c3aed",
}


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def fmt_pct(value: object, signed: bool = True) -> str:
    if value is None:
        return "—"
    number = float(value)
    return f"{number:+.2f}%" if signed else f"{number:.2f}%"


def fmt_decimal_return(value: object, signed: bool = True) -> str:
    # 缠论结构波段的 *_gain_pct 字段以 decimal_return（小数收益率）存储，
    # 例如 0.9625 表示 96.25%；渲染为百分比需 ×100，不可与百分比量纲的值混用。
    if value is None:
        return "—"
    number = float(value) * 100.0
    return f"{number:+.2f}%" if signed else f"{number:.2f}%"


def logical_source(value: object, fallback: str) -> str:
    if not value:
        return fallback
    path = str(value).replace("\\", "/")
    if "/cache/" in path:
        return "cache/" + path.split("/cache/", 1)[1]
    return Path(path).name or fallback


def coverage_from_manifest(source: dict[str, Any]) -> tuple[int, int, int]:
    path_value = source.get("bootstrap_manifest")
    if path_value:
        try:
            payload = json.loads(Path(str(path_value)).read_text(encoding="utf-8"))
            if isinstance(payload, list):
                ok = sum(1 for row in payload if row.get("status") == "ok")
                return ok, len(payload), len(payload) - ok
            summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
            universe = int(summary.get("universe_size", 0) or 0)
            failed = int(summary.get("failed_symbols", 0) or 0)
            if universe:
                return max(universe - failed, 0), universe, failed
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    failed = int(source.get("bootstrap_failed_symbols", 0) or 0)
    return 0, 0, failed


def build_metrics(data: dict[str, Any]) -> list[dict[str, Any]]:
    dates = [str(day) for day in data.get("dates", [])]
    qualified = list(data.get("qualified", []))
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in qualified:
        rows_by_date[str(row.get("date"))].append(row)
    supplied = {str(item.get("date")): item for item in data.get("daily_summary", [])}
    metrics: list[dict[str, Any]] = []
    previous_code = ""
    leader_streak = 0
    direction_streak = 0
    previous_direction = 0
    for index, day in enumerate(dates):
        summary = supplied.get(day, {})
        day_rows = sorted(rows_by_date.get(day, []), key=lambda row: (-float(row.get("pct", 0)), str(row.get("ts_code", ""))))
        leader = summary.get("leader") or (day_rows[0] if day_rows else {})
        maximum = summary.get("maximum_return_10d")
        if maximum is None and leader:
            maximum = float(leader.get("pct", 0))
        top3 = summary.get("top3") or day_rows[:3]
        top3_values = [float(row.get("pct", 0)) for row in top3]
        top3_median = summary.get("top3_median_return_10d")
        if top3_median is None:
            top3_median = median(top3_values) if top3_values else None
        code = str(leader.get("ts_code", ""))
        changed = bool(previous_code and code and code != previous_code)
        if code and code == previous_code:
            leader_streak += 1
        elif code:
            leader_streak = 1
        else:
            leader_streak = 0
        previous_maximum = metrics[-1]["maximum"] if metrics else None
        change = float(maximum) - float(previous_maximum) if maximum is not None and previous_maximum is not None else None
        direction = 1 if change is not None and change > 1e-9 else -1 if change is not None and change < -1e-9 else 0
        if direction and direction == previous_direction:
            direction_streak += 1
        elif direction:
            direction_streak = 1
        else:
            direction_streak = 0
        if direction:
            previous_direction = direction
        market_maxima = summary.get("market_maxima") or {}
        market_group_maxima = summary.get("market_group_maxima") or {}
        if not market_group_maxima:
            for group, source_markets in MARKET_GROUP_SOURCES.items():
                candidates = [row for row in day_rows if row.get("market") in source_markets]
                if candidates:
                    market_group_maxima[group] = candidates[0]
        metrics.append({
            "date": day,
            "base_date": summary.get("base_date") or (leader.get("base_date") if leader else ""),
            "maximum": maximum,
            "change": change,
            "direction": direction,
            "direction_streak": direction_streak,
            "leader_name": leader.get("name", "—"),
            "leader_code": code or "—",
            "leader_market": leader.get("market", "—"),
            "leader_changed": bool(summary.get("leader_changed", changed)),
            "leader_streak": int(summary.get("leader_streak_days", leader_streak) or 0),
            "top3_median": top3_median,
            "count_ge_threshold": int(summary.get("qualified_count", len(day_rows)) or 0),
            "market_maxima": market_maxima,
            "market_group_maxima": market_group_maxima,
        })
        previous_code = code or previous_code
    return metrics


def classify_state(metrics: list[dict[str, Any]]) -> tuple[str, str]:
    if not metrics:
        return "数据不足", "没有足够数据判断短线情绪高度。"
    latest = metrics[-1]
    changes = [item["change"] for item in metrics[-3:] if item["change"] is not None]
    positive = sum(1 for value in changes if value > 0)
    negative = sum(1 for value in changes if value < 0)
    if latest["change"] is not None and latest["change"] > 0 and positive >= 2:
        return "高度扩张", "近端最大值以抬升为主，短线资金正在拓展可接受的高度。"
    if latest["change"] is not None and latest["change"] > 0:
        return "情绪修复", "最新最大值回升，但仍需观察能否连续抬高并由更多前排股票确认。"
    if negative >= 2:
        return "高度收缩", "近端最大值持续回落，短线资金对高位空间的拓展意愿下降。"
    return "高位分歧", "最大值缺乏连续方向，榜首高度处于震荡或标杆切换阶段。"


def emotion_svg(metrics: list[dict[str, Any]], series_name: str) -> str:
    if not metrics:
        return '<div class="empty">无可绘制数据</div>'
    points_data: list[dict[str, Any]] = []
    for item in metrics:
        if series_name == "全市":
            value = item.get("maximum")
            leader_name = item.get("leader_name", "—")
            leader_code = item.get("leader_code", "—")
            leader_market = item.get("leader_market", "—")
        else:
            group_row = item.get("market_group_maxima", {}).get(series_name, {})
            value = group_row.get("pct")
            leader_name = group_row.get("name", "—")
            leader_code = group_row.get("ts_code", "—")
            leader_market = group_row.get("market", "—")
        if value is not None:
            points_data.append({
                "date": item["date"],
                "value": float(value),
                "leader_name": leader_name,
                "leader_code": leader_code,
                "leader_market": leader_market,
            })
    if not points_data:
        return '<div class="empty">无可绘制数据</div>'
    values = [point["value"] for point in points_data]
    width, height = 1160, 390
    left, right, top, bottom = 72, 44, 42, 82
    plot_w, plot_h = width - left - right, height - top - bottom
    lower = min(0.0, math.floor(min(values) / 20) * 20)
    upper = max(100.0, math.ceil(max(values) / 20) * 20 + 10)
    span = max(upper - lower, 1)
    step = plot_w / max(len(points_data) - 1, 1)
    line_color = SERIES_COLORS["总体极值" if series_name == "全市" else series_name]
    parts = [
        f'<svg class="trend-svg" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(series_name)}每日10日涨幅最大值与短线情绪高度">',
        f'<rect width="{width}" height="{height}" rx="14" fill="#fff"/>',
    ]
    for i in range(6):
        y = top + plot_h * i / 5
        value = upper - span * i / 5
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{left-12}" y="{y+4:.1f}" text-anchor="end" class="axis">{value:.0f}%</text>')
    for reference in (50, 100):
        if lower <= reference <= upper:
            y = top + plot_h - (reference - lower) / span * plot_h
            parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#94a3b8" stroke-dasharray="4 6"/>')
            parts.append(f'<text x="{width-right}" y="{y-7:.1f}" text-anchor="end" class="axis">{reference}%参考线</text>')
    plotted = []
    for index, point in enumerate(points_data):
        x = left + step * index
        y = top + plot_h - (point["value"] - lower) / span * plot_h
        plotted.append((x, y, point))
    path = " ".join(("M" if index == 0 else "L") + f" {x:.1f} {y:.1f}" for index, (x, y, _) in enumerate(plotted))
    parts.append(f'<path d="{path}" fill="none" stroke="{line_color}" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>')
    peak_index = max(range(len(plotted)), key=lambda index: plotted[index][2]["value"])
    low_index = min(range(len(plotted)), key=lambda index: plotted[index][2]["value"])
    for index, (x, y, point) in enumerate(plotted):
        previous_value = plotted[index - 1][2]["value"] if index else None
        change = point["value"] - previous_value if previous_value is not None else None
        point_color = "#dc2626" if change is None or change >= 0 else "#16835a"
        change_text = fmt_pct(change) if change is not None else "首日"
        title = f'{point["date"]}；最大值{fmt_pct(point["value"])}；环比{change_text}；榜首{point["leader_name"]} {point["leader_code"]}；{point["leader_market"]}'
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{point_color}" stroke="#fff" stroke-width="2"><title>{esc(title)}</title></circle>')
        label_y = y - 16 if index % 2 == 0 else y + 25
        parts.append(f'<text x="{x:.1f}" y="{label_y:.1f}" text-anchor="middle" class="leader-label">{esc(point["leader_name"])}</text>')
        parts.append(f'<text x="{x:.1f}" y="{top+plot_h+24:.1f}" text-anchor="middle" class="date-label">{esc(point["date"][5:])}</text>')
        if index in {peak_index, low_index, len(plotted) - 1}:
            mark = "期内最高" if index == peak_index else "期内最低" if index == low_index else "最新"
            parts.append(f'<text x="{x:.1f}" y="{max(16,y-34):.1f}" text-anchor="middle" class="key-note">{mark} {point["value"]:.1f}%</text>')
    parts.append(f'<text x="20" y="{top+plot_h/2:.1f}" transform="rotate(-90 20 {top+plot_h/2:.1f})" text-anchor="middle" class="axis-title">10日涨幅最大值</text>')
    parts.append('</svg>')
    return "".join(parts)


def breadth_svg(metrics: list[dict[str, Any]], threshold: float) -> str:
    if not metrics:
        return '<div class="empty">无可绘制数据</div>'
    width, height = 1160, 260
    left, right, top, bottom = 64, 38, 30, 62
    plot_w, plot_h = width - left - right, height - top - bottom
    maximum = max(item["count_ge_threshold"] for item in metrics) or 1
    upper = maximum + max(1, int(maximum * .25))
    step = plot_w / len(metrics)
    bar_width = min(58, step * .58)
    parts = [f'<svg class="trend-svg" viewBox="0 0 {width} {height}" role="img" aria-label="阈值达标股票数量辅助趋势">', f'<rect width="{width}" height="{height}" rx="14" fill="#fff"/>']
    for i in range(5):
        y = top + plot_h * i / 4
        value = upper * (4-i) / 4
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" class="axis">{value:.0f}</text>')
    for index, item in enumerate(metrics):
        x = left + step * (index + .5)
        height_value = plot_h * item["count_ge_threshold"] / upper
        y = top + plot_h - height_value
        parts.append(f'<rect x="{x-bar_width/2:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{height_value:.1f}" rx="4" fill="#f59e0b"><title>{esc(item["date"])}：10日涨幅≥{threshold:g}%共{item["count_ge_threshold"]}只</title></rect>')
        parts.append(f'<text x="{x:.1f}" y="{y-7:.1f}" text-anchor="middle" class="count-label">{item["count_ge_threshold"]}</text>')
        parts.append(f'<text x="{x:.1f}" y="{top+plot_h+22:.1f}" text-anchor="middle" class="date-label">{esc(item["date"][5:])}</text>')
    parts.append('</svg>')
    return "".join(parts)


def status_distribution(rows: list[dict[str, Any]], status_key: str = "status") -> dict[str, int]:
    counts = Counter(str(row.get(status_key) or "unknown") for row in rows)
    return {status: counts.get(status, 0) for status in ("confirmed", "pending", "data_insufficient", "rejected")}


STATUS_LABELS = {
    "confirmed": ("已确认", "good"),
    "pending": ("待确认", "warn"),
    "data_insufficient": ("数据不足", ""),
    "rejected": ("不达标", ""),
}


def status_badge(value: object) -> str:
    label, klass = STATUS_LABELS.get(str(value), (str(value or "—"), ""))
    return f'<span class="badge {klass}">{esc(label)}</span>'


def structure_section_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty">未提供缠论结构波段数据。</div>'
    counts = status_distribution(rows)
    parts = [
        f'<p class="muted">状态分布：已确认 {counts["confirmed"]} · 待确认 {counts["pending"]} · 数据不足 {counts["data_insufficient"]} · 不达标 {counts["rejected"]}。'
        '全部候选均列出（不按状态过滤）；待确认表示已出现起点但区间内尚未完成顶部确认，其高度字段留空即为未确认，不强行估算。</p>',
    ]
    body = "".join(
        f'<tr><td>{esc(row.get("code"))}</td><td><strong>{esc(row.get("name"))}</strong></td>'
        f'<td>{esc(row.get("market"))}</td><td>{esc(row.get("start_date"))}</td>'
        f'<td>{esc(row.get("start_confirm_date"))}</td><td>{esc(row.get("end_date") or "—")}</td>'
        f'<td>{esc(row.get("end_confirm_date") or "—")}</td>'
        f'<td class="up">{fmt_decimal_return(row.get("theoretical_gain_pct"))}</td>'
        f'<td>{fmt_decimal_return(row.get("observable_gain_pct"))}</td>'
        f'<td>{fmt_decimal_return(row.get("dual_confirm_gain_pct"))}</td>'
        f'<td>{status_badge(row.get("status"))}</td></tr>'
        for row in sorted(rows, key=lambda item: (-(float(item.get("theoretical_gain_pct") or 0)), str(item.get("code") or "")))
    )
    parts.append(
        '<div class="table-wrap"><table><thead><tr>'
        '<th>代码</th><th>名称</th><th>市场</th><th>起点</th><th>起点确认</th><th>终点</th><th>终点确认</th>'
        '<th>理论高度</th><th>确认后高度</th><th>双端确认</th><th>状态</th>'
        '</tr></thead><tbody>' + body + '</tbody></table></div>'
    )
    return "".join(parts)


def emotion_section_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty">未提供首板情绪波段数据。</div>'
    counts = status_distribution(rows)
    parts = [
        f'<p class="muted">状态分布：已确认 {counts["confirmed"]} · 待确认 {counts["pending"]} · 数据不足 {counts["data_insufficient"]} · 不达标 {counts["rejected"]}。'
        '全部候选均列出（不按状态过滤）；首板情绪波段以显式涨停状态识别首板，跟踪前复权最高价，连续 4 个交易日不创新高确认结束。</p>',
    ]
    body = "".join(
        f'<tr><td>{esc(row.get("code"))}</td><td><strong>{esc(row.get("name"))}</strong></td>'
        f'<td>{esc(row.get("first_board_date"))}</td><td>{esc(row.get("high_date"))}</td>'
        f'<td>{esc(row.get("trend_end_date") or "—")}</td>'
        f'<td class="up">{fmt_pct(row.get("rally_gain_pct"))}</td>'
        f'<td>{esc(row.get("rally_days"))}</td><td>{esc(row.get("style"))}</td>'
        f'<td>{status_badge(row.get("status"))}</td></tr>'
        for row in sorted(rows, key=lambda item: (-(float(item.get("rally_gain_pct") or 0)), str(item.get("code") or "")))
    )
    parts.append(
        '<div class="table-wrap"><table><thead><tr>'
        '<th>代码</th><th>名称</th><th>首板日</th><th>最高日</th><th>结束日</th><th>波段高度</th><th>天数</th><th>风格</th><th>状态</th>'
        '</tr></thead><tbody>' + body + '</tbody></table></div>'
    )
    return "".join(parts)


def load_forward_close(directory: Path | None) -> dict[str, dict[str, float]]:
    """读取前复权日线目录，返回 {code: {trade_date: close}}；code 取文件名 6 位裸代码。"""
    out: dict[str, dict[str, float]] = {}
    if not directory:
        return out
    root = Path(directory)
    if not root.is_dir():
        return out
    for file in sorted(root.glob("*.json")):
        try:
            rows = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        code = file.stem.split(".")[0]
        series: dict[str, float] = {}
        for row in rows:
            day, close = row.get("trade_date"), row.get("close")
            if day is None or close is None:
                continue
            try:
                series[str(day)] = float(close)
            except (TypeError, ValueError):
                continue
        if series:
            out[code] = series
    return out


def build_pool_trends(
    dates: list[str],
    qualified: list[dict[str, Any]],
    structure_rows: list[dict[str, Any]],
    emotion_rows: list[dict[str, Any]],
    forward_close: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """构建达标池三口径每日序列。

    P(t) = { code | 10日涨幅(t) >= 阈值 }，直接由 qualified 按 date 分组得到（快照天然复用）。
    T1 = 池内 max(10日涨幅)；T2 = 池内 max(当日收盘/缠论起点收盘 - 1)；T3 = 池内 max(当日收盘/首板日收盘 - 1)。
    """
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in qualified:
        by_date[str(row.get("date"))].append(row)
    start_map = {str(r.get("code")): str(r.get("start_date")) for r in structure_rows if r.get("start_date") and r.get("code")}
    board_map = {str(r.get("code")): str(r.get("first_board_date")) for r in emotion_rows if r.get("first_board_date") and r.get("code")}

    ever: set[str] = set()
    days: list[dict[str, Any]] = []
    for day in dates:
        members = by_date.get(day, [])
        codes = [str(m.get("code")) for m in members if m.get("code")]
        ever.update(codes)
        t1_values: list[float] = []
        t2_values: list[float] = []
        t3_values: list[float] = []
        for member in members:
            code = str(member.get("code") or "")
            if not code:
                continue
            pct = member.get("pct")
            if pct is not None:
                try:
                    t1_values.append(float(pct) / 100.0)
                except (TypeError, ValueError):
                    pass
            series = forward_close.get(code) or {}
            price = series.get(day)
            if price is None:
                continue
            anchor = start_map.get(code)
            if anchor and day >= anchor and series.get(anchor):
                t2_values.append(price / series[anchor] - 1)
            board = board_map.get(code)
            if board and day >= board and series.get(board):
                t3_values.append(price / series[board] - 1)
        days.append({
            "date": day,
            "count": len(members),
            "y1": max(t1_values) if t1_values else None,
            "y2": max(t2_values) if t2_values else None,
            "y3": max(t3_values) if t3_values else None,
        })

    anchors = [start_map[c] for c in ever if c in start_map] + [board_map[c] for c in ever if c in board_map]
    return {
        "days": days,
        "x_start": min(anchors) if anchors else (dates[0] if dates else None),
        "coverage": {
            "total": len(ever),
            "structure": sum(1 for c in ever if c in start_map),
            "first_board": sum(1 for c in ever if c in board_map),
            "with_price": sum(1 for c in ever if forward_close.get(c)),
        },
    }


POOL_SERIES = [
    ("y1", "T1 短线情绪（10日涨幅最大值）", "#98a2b3", 1.8, "池内 max(10日涨幅) · 与原「全市」图等价，作参考基线", True),
    ("y2", "T2 缠论结构波段（波段涨幅最大值）", "#dc2626", 2.6, "池内 max(当日收盘 / 缠论起点收盘 − 1)", False),
    ("y3", "T3 首板情绪波段（波段涨幅最大值）", "#7c3aed", 2.6, "池内 max(当日收盘 / 首板日收盘 − 1)", False),
]


def pool_trend_svg(pool: dict[str, Any]) -> str:
    """达标池三口径三联图：共享 x 轴、共用 y 轴上界。"""
    days_all = pool.get("days") or []
    x_start = pool.get("x_start")
    shown = [d for d in days_all if x_start is None or d["date"] >= x_start]
    if not shown:
        return '<p class="muted">本期无达标股票池，无法生成达标池趋势。</p>'
    available = [d[key] for d in shown for key, *_ in POOL_SERIES if d.get(key) is not None]
    if not available:
        return '<p class="muted">达标池存在，但缺少前复权日线或波段起点，无法计算波段涨幅；请提供 <code>--forward-daily</code>。</p>'
    y_max = max(0.1, (math.floor(max(available) * 10) + 1) / 10.0)

    width, left, right = 1000, 62, 976
    panel_h, gap, top0 = 126, 48, 26
    total_h = top0 + 3 * panel_h + 2 * gap + 56
    count = len(shown)

    def x_at(index: int) -> float:
        return left + (index / (count - 1)) * (right - left) if count > 1 else (left + right) / 2

    parts = [f'<svg viewBox="0 0 {width} {total_h}" width="100%" class="trend-svg" role="img" aria-label="达标池三口径趋势">']
    for order, (key, label, color, stroke_w, note, dashed) in enumerate(POOL_SERIES):
        top = top0 + order * (panel_h + gap)
        bottom = top + panel_h

        def y_at(value: float, bottom: float = bottom) -> float:
            return bottom - (value / y_max) * panel_h

        parts.append(f'<text x="{left}" y="{top-10}" font-size="12.5" font-weight="700" fill="{color}">{esc(label)}</text>')
        parts.append(f'<text x="{right}" y="{top-10}" text-anchor="end" font-size="11" fill="#667085">{esc(note)}</text>')
        for step in range(5):
            value = y_max * step / 4
            y = y_at(value)
            parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#eef0f3"/>')
            parts.append(f'<text x="{left-8}" y="{y+3.5:.1f}" text-anchor="end" class="axis">{value*100:.0f}%</text>')
        points = [(index, d[key]) for index, d in enumerate(shown) if d.get(key) is not None]
        if points:
            dash = ' stroke-dasharray="5 4"' if dashed else ""
            path = " ".join(("M" if j == 0 else "L") + f" {x_at(i):.1f} {y_at(v):.1f}" for j, (i, v) in enumerate(points))
            parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{stroke_w}" stroke-linejoin="round" stroke-linecap="round"{dash}/>')
            peak_point = max(points, key=lambda item: item[1])
            px, py = x_at(peak_point[0]), y_at(peak_point[1])
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.8" fill="{color}" stroke="#fff" stroke-width="1.5"><title>{esc(shown[peak_point[0]]["date"])}：{peak_point[1]*100:.2f}%</title></circle>')
            parts.append(f'<text x="{px:.1f}" y="{py-9:.1f}" text-anchor="middle" font-size="11" font-weight="700" fill="{color}">{peak_point[1]*100:.1f}%</text>')
        parts.append(f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#d0d5dd"/>')
        if order == len(POOL_SERIES) - 1:
            for index, day in enumerate(shown):
                if index % 5 == 0 or index == count - 1:
                    parts.append(f'<text x="{x_at(index):.1f}" y="{bottom+22}" text-anchor="middle" class="date-label">{esc(day["date"][5:])}</text>')
            parts.append(f'<text x="{(left+right)/2:.0f}" y="{bottom+44}" text-anchor="middle" class="date-label">交易日（三图共享同一 x 轴 · 共用 y 轴上界 {y_max*100:.0f}%，可横向比较）</text>')
    parts.append("</svg>")
    return "".join(parts)


def pool_trend_meta_html(pool: dict[str, Any], threshold: float) -> str:
    coverage = pool.get("coverage") or {}
    days_all = pool.get("days") or []
    latest_count = days_all[-1]["count"] if days_all else 0
    x_start = pool.get("x_start") or "—"
    total = coverage.get("total", 0)
    return (
        f'<div class="callout"><strong>快照口径：</strong>阈值 ≥ {threshold:g}% ｜ 最新池规模 {latest_count} 只 ｜ '
        f'时间轴起点 {esc(x_start)}（池内最早波段起点）｜ 波段起算「起点 → 当日收盘」｜ '
        f'覆盖：缠论起点 {coverage.get("structure", 0)}/{total} 只、首板日 {coverage.get("first_board", 0)}/{total} 只、'
        f'有日线 {coverage.get("with_price", 0)}/{total} 只。</div>'
    )


def render(data: dict[str, Any], forward_close: dict[str, dict[str, float]] | None = None) -> str:
    metrics = build_metrics(data)
    dates = [str(day) for day in data.get("dates", [])]
    qualified = list(data.get("qualified", []))
    structure_rows = list(data.get("structure_waves") or [])
    emotion_rows = list(data.get("emotion_rallies") or [])
    source = dict(data.get("source", {}))
    validation = dict(data.get("validation", {}))
    threshold = float(data.get("min_return_pct", 50))
    latest = metrics[-1] if metrics else {}
    previous = metrics[-2] if len(metrics) > 1 else {}
    valid_metrics = [item for item in metrics if item.get("maximum") is not None]
    peak = max(valid_metrics, key=lambda item: float(item["maximum"]), default={"maximum": 0, "date": "—"})
    low = min(valid_metrics, key=lambda item: float(item["maximum"]), default={"maximum": 0, "date": "—"})
    state, state_detail = classify_state(metrics)
    leader_switches = sum(1 for item in metrics if item["leader_changed"])
    ok_count, universe_count, failed_count = coverage_from_manifest(source)
    effective_failed = int(source.get("bootstrap_effective_failed_symbols", failed_count) or 0)
    coverage = f"{ok_count}/{universe_count}" if universe_count else "见数据源"
    validation_ok = validation.get("valid") is True
    st_coverage = str(source.get("st_coverage", "none"))
    range_text = f"{dates[0]} 至 {dates[-1]}" if dates else "无数据"
    latest_change = latest.get("change")
    latest_direction = "抬升" if latest_change is not None and latest_change > 0 else "回落" if latest_change is not None and latest_change < 0 else "持平"
    tldr = (
        f"当前状态判断为“{state}”。最新交易日10日涨幅榜最大值为 {fmt_pct(latest.get('maximum'))}，"
        f"由{esc(latest.get('leader_name','—'))}（{esc(latest.get('leader_code','—'))}）贡献，较前一交易日{latest_direction} {fmt_pct(abs(float(latest_change)) if latest_change is not None else None, signed=False)}。"
        f"观察期最高值为 {fmt_pct(peak.get('maximum'))}（{esc(peak.get('date'))}），最低值为 {fmt_pct(low.get('maximum'))}（{esc(low.get('date'))}）；"
        f"榜首共切换 {leader_switches} 次。{state_detail}"
    )
    trend_rows = "".join(
        f'<tr><td>{esc(item["date"])}</td><td class="up"><strong>{fmt_pct(item["maximum"])}</strong></td><td class="{"up" if item["direction"]>0 else "down" if item["direction"]<0 else ""}">{fmt_pct(item["change"])}</td><td><strong>{esc(item["leader_name"])}</strong><small>{esc(item["leader_code"])}</small></td><td>{esc(item["leader_market"])}</td><td>{"是" if item["leader_changed"] else "否"}</td><td>{item["leader_streak"]}</td><td>{fmt_pct(item["top3_median"])}</td><td>{item["count_ge_threshold"]}</td></tr>'
        for item in metrics
    )
    market_header = "".join(f"<th>{esc(market)}</th>" for market in MARKET_GROUP_ORDER)
    market_rows = "".join(
        "<tr>" + f'<td>{esc(item["date"])}</td>' + "".join(
            f'<td>{fmt_pct(item["market_group_maxima"].get(market, {}).get("pct"))}<small>{esc(item["market_group_maxima"].get(market, {}).get("name", "—"))} · {esc(item["market_group_maxima"].get(market, {}).get("ts_code", "—"))}</small></td>'
            for market in MARKET_GROUP_ORDER
        ) + "</tr>"
        for item in metrics
    )
    latest_rows = sorted((row for row in qualified if str(row.get("date")) == str(latest.get("date"))), key=lambda row: float(row.get("pct", 0)), reverse=True)
    latest_top = "".join(
        f'<tr><td>{index}</td><td><strong>{esc(row.get("name"))}</strong><small>{esc(row.get("ts_code"))}</small></td><td>{esc(row.get("market"))}</td><td class="up">{fmt_pct(row.get("pct"))}</td></tr>'
        for index, row in enumerate(latest_rows[:10], 1)
    ) or '<tr><td colspan="4">最新交易日无阈值达标股票。</td></tr>'
    source_items = [
        ("交易日历", logical_source(source.get("calendar"), "cache/calendar.json")),
        ("后复权行情", logical_source(source.get("daily"), "cache/daily_adjusted/")),
        ("股票主数据", logical_source(source.get("stock_basic"), "cache/stock_basic.json")),
        ("历史缓存清单", logical_source(source.get("bootstrap_manifest"), "cache/bootstrap_manifest.json")),
    ]
    source_html = "".join(f'<li><span>{esc(label)}</span><code>{esc(path)}</code></li>' for label, path in source_items)
    process_html = "".join(f'<tr><td>{esc(row.get("step"))}</td><td>{esc(row.get("component"))}</td><td>{esc(row.get("purpose"))}</td><td>{esc(row.get("status"))}</td></tr>' for row in data.get("process_log", []))
    audit_excluded = list(data.get("audit_excluded", []))
    audit_html = "".join(f'<tr><td>{esc(row.get("date"))}</td><td>{esc(row.get("name"))}</td><td>{esc(row.get("ts_code"))}</td><td>{fmt_pct(row.get("pct"))}</td><td>{esc(row.get("exclusion_reason"))}</td></tr>' for row in audit_excluded) or '<tr><td colspan="5">本期无阈值后资格排除记录。</td></tr>'
    pool = build_pool_trends(dates, qualified, structure_rows, emotion_rows, forward_close or {})
    template = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title><style>
:root{--red:#dc2626;--green:#16835a;--ink:#172033;--muted:#667085;--line:#e5e7eb;--soft:#f6f7fb;--amber:#f59e0b}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--soft);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif}.wrap{max-width:1380px;margin:auto;padding:24px 22px 56px}.hero,.panel,.card{background:#fff;border:1px solid var(--line);border-radius:16px}.hero{padding:28px;border-top:5px solid var(--red)}h1{font-size:30px;line-height:1.25;margin:6px 0 10px}h2{font-size:21px;margin:0 0 8px}h3{font-size:16px;margin:0 0 6px}.eyebrow{font-size:13px;color:#475467;font-weight:700}.subtitle{font-size:16px;line-height:1.7;margin:0 0 6px;color:#344054}.muted,small{color:var(--muted);font-size:13px;line-height:1.65}.tag,.badge{display:inline-block;border:1px solid #dfe3e8;border-radius:999px;padding:6px 10px;margin:4px 4px 0 0;font-size:12px}.badge.good{color:#087443;background:#ecfdf3}.badge.warn{color:#9a3412;background:#fff7ed}td .badge{padding:2px 8px;margin:0;font-size:11.5px;white-space:nowrap}.tldr{margin-top:18px;border-left:5px solid var(--red);background:#fff7f3;border-radius:10px;padding:16px 18px;font-size:16px;line-height:1.8}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px;margin-top:16px}.card{padding:16px}.card b{display:block;font-size:27px;margin:6px 0}.panel{padding:20px;margin-top:16px}.panel-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.chart-title{margin-top:24px;padding-top:20px;border-top:1px solid var(--line)}.nav{position:sticky;top:0;z-index:5;display:flex;gap:14px;overflow:auto;background:#fffffff2;border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:14px 0}.nav a{color:#344054;text-decoration:none;white-space:nowrap;font-size:13px}.trend-svg{display:block;width:100%;height:auto}.chart{overflow:auto;border:1px solid #edf0f3;border-radius:12px;margin-top:14px}.axis,.date-label{font-size:11px;fill:#667085}.axis-title,.count-label,.leader-label{font-size:12px;fill:#344054;font-weight:600}.key-note{font-size:11px;fill:#b42318;font-weight:700}.up{color:var(--red)}.down{color:var(--green)}.table-wrap{overflow:auto;margin-top:12px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #e8ebf0;padding:10px 11px;text-align:left;white-space:nowrap}th{background:#f8fafc;color:#475467;position:sticky;top:0}td small{display:block}.callout{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:13px 15px;color:#854d0e;margin-top:12px}.source-list{list-style:none;padding:0}.source-list li{display:flex;justify-content:space-between;gap:16px;padding:8px 0;border-bottom:1px solid var(--line)}details{border-top:1px solid var(--line);padding:13px 0}summary{cursor:pointer;font-weight:700}.disclaimer{font-size:12px;color:#667085;border-top:1px solid var(--line);padding-top:14px}.empty{padding:30px;text-align:center;color:#667085}@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.wrap{padding:14px}h1{font-size:24px}}@media print{body{background:#fff}.nav{display:none}.panel,.hero{break-inside:avoid;box-shadow:none}.wrap{max-width:none;padding:0}}
</style></head><body><main class="wrap"><header class="hero" id="summary"><div class="eyebrow">A股短线情绪高度跟踪 · 数据截至 __LATEST__ 收盘</div><h1>__TITLE__</h1><p class="subtitle"><strong>__SUBTITLE__</strong></p><p class="muted">数据说明：统计沪市主板、深市主板、科创板和创业板，合并为“沪深主板”与“科创成长板（科创板+创业板）”；剔除ST、*ST、退市、退市整理及上市不足__NEW_DAYS__个自然日的新股。以统一后复权收盘价计算10个交易日区间涨幅。</p><div><span class="tag">__DATE_RANGE__</span><span class="tag">10日后复权收益</span><span class="tag">沪深主板</span><span class="tag">科创成长板</span><span class="tag">不含北交所</span><span class="tag">辅助阈值 ≥ __THRESHOLD__%</span></div><div class="tldr"><strong>结论：</strong>__TLDR__</div><section class="grid"><div class="card"><span class="muted">当前情绪状态</span><b>__STATE__</b><span class="muted">由极值趋势判定</span></div><div class="card"><span class="muted">最新10日涨幅最大值</span><b class="up">__LATEST_MAX__</b><span class="__CHANGE_CLASS__">较前日 __LATEST_CHANGE__</span></div><div class="card"><span class="muted">最新榜首</span><b>__LATEST_LEADER__</b><span class="muted">__LATEST_CODE__ · __LATEST_MARKET__</span></div><div class="card"><span class="muted">观察期高低区间</span><b>__LOW__—__PEAK__</b><span class="muted">榜首切换 __SWITCHES__ 次</span></div></section><div><span class="badge __VALID_CLASS__">结果：__VALID__</span><span class="badge __ST_CLASS__">历史ST：__ST_LABEL__</span><span class="badge good">行情覆盖：__COVERAGE__</span><span class="badge __FAIL_CLASS__">有效缺失：__FAILED__只</span></div></header><nav class="nav"><a href="#summary">情绪结论</a><a href="#height">高度趋势</a><a href="#pool-trends">达标池趋势</a><a href="#leaders">逐日榜首</a><a href="#breadth">广度验证</a><a href="#markets">分市场</a><a href="#latest">最新前排</a><a href="#structure">缠论结构波段</a><a href="#emotion">首板情绪波段</a><a href="#method">口径审计</a></nav>
<section class="panel" id="height"><div class="panel-head"><div><h2>每日10日涨幅最大值与短线情绪高度</h2><p class="muted">按全市、沪深主板、科创成长板拆分为三张独立趋势图，分别观察总体情绪天花板、主板接力高度与成长风格弹性，避免三条曲线叠加造成视觉干扰。</p></div></div><h3>全市短线情绪高度</h3><p class="muted">全市特指本报告四板块合并后的有效股票池，不含北交所。数据点标注当日总体榜首。</p><div class="chart">__OVERALL_EMOTION_CHART__</div><h3 class="chart-title">沪深主板短线情绪高度</h3><p class="muted">统计沪市主板与深市主板，观察10%涨跌幅市场的短线高度变化。</p><div class="chart">__MAINBOARD_EMOTION_CHART__</div><h3 class="chart-title">科创成长板短线情绪高度</h3><p class="muted">统计科创板与创业板，观察20%涨跌幅市场的成长风格短线高度变化。</p><div class="chart">__GROWTH_EMOTION_CHART__</div><div class="callout"><strong>解读原则：</strong>同一市场组合内最大值连续抬升表示高度扩张，连续回落表示高度收缩；两组同步抬升代表共振，一升一降代表风格分化。全市最大值等于两个组合市场最大值中的较高者；主板与双创涨跌幅制度不同，绝对数值不可机械横向比较。</div></section>
<section class="panel" id="pool-trends"><div class="panel-head"><div><h2>达标池三口径趋势</h2><p class="muted">在每日达标股票池 P(t)（10日涨幅≥__THRESHOLD__%）内，分别取三种「波段涨幅」的每日最大值。三图共享同一份快照池、同一 x 轴与同一 y 轴量纲，可直接横向比较。</p></div></div>__POOL_TREND_META__<div class="chart">__POOL_TREND_CHART__</div><div class="callout"><strong>解读原则：</strong>① T1 为参考基线，与原「全市短线情绪高度」在阈值以上区段等价，用于判断池内极值是否仍由同一标杆支撑；② T2/T3 是「起点→当日」的累计波段涨幅，跨期更长，高于 T1 属正常，两者差距反映结构启动与情绪启动的时间差；③ 三线同步抬升代表情绪共振，T2/T3 抬升而 T1 回落代表池内已启动个股在延续、但新进池个股转弱；④ 某股无波段起点或日线时不进入对应曲线，覆盖数见上方口径行。</div></section>
<section class="panel" id="leaders"><h2>逐日情绪高度与榜首切换</h2><p class="muted">这是报告的核心业务表。前3中位数用于判断最大值是否孤立，≥阈值股票数用于判断高度是否获得更多强势股支持。</p><div class="table-wrap"><table><thead><tr><th>交易日</th><th>10日涨幅最大值</th><th>环比</th><th>榜首股票</th><th>市场</th><th>榜首切换</th><th>连续榜首</th><th>前3中位数</th><th>≥__THRESHOLD__%数量</th></tr></thead><tbody>__TREND_ROWS__</tbody></table></div></section>
<section class="panel" id="breadth"><h2>辅助验证：达到__THRESHOLD__%的股票数量</h2><p class="muted">本图仅验证极值背后是否存在强势股扩散，不再作为报告主线。若最大值上升但达标数量下降，需警惕高度由单一标杆支撑。</p><div class="chart">__BREADTH_CHART__</div></section>
<section class="panel" id="markets"><h2>两个市场组合的每日最大值</h2><p class="muted">“沪深主板”包含沪市主板和深市主板；“科创成长板”包含科创板和创业板。北交所及其他市场不纳入统计。两组涨跌幅制度不同，应分别观察趋势与拐点。</p><div class="table-wrap"><table><thead><tr><th>交易日</th>__MARKET_HEADER__</tr></thead><tbody>__MARKET_ROWS__</tbody></table></div></section>
<section class="panel" id="latest"><h2>最新交易日前排：验证榜首是否孤立</h2><p class="muted">__LATEST__ 达到辅助阈值的股票，按10日涨幅降序。高涨幅是历史路径描述，不是追涨或买入信号。</p><div class="table-wrap"><table><thead><tr><th>排名</th><th>股票</th><th>市场</th><th>10日涨幅</th></tr></thead><tbody>__LATEST_TOP__</tbody></table></div></section>
<section class="panel" id="structure"><div class="panel-head"><div><h2>缠论结构波段高度</h2><p class="muted">针对达到辅助阈值的候选个股，逐股计算“严格底分型 + MA5 两日确认起点、严格顶分型 + MA5 两日确认终点”的结构波段；该计算只依赖 OHLC 相对关系与 MA5，与前复权/后复权整体缩放无关。</p></div></div><div class="callout"><strong>解读原则：</strong>已确认波段给出从结构低点到结构高点的理论高度；待确认表示已出现起点但区间内尚未完成顶部确认；数据不足表示日线或字段不完整，绝不强行估算。</div>__STRUCTURE_SECTION__</section>
<section class="panel" id="emotion"><div class="panel-head"><div><h2>首板情绪波段高度</h2><p class="muted">以显式涨停状态识别首板起点，跟踪前复权最高价，连续 4 个交易日不创新高确认结束；涨停状态仅来自上游数据，绝不从涨跌幅或收盘价推断。</p></div></div><div class="callout"><strong>解读原则：</strong>已确认波段给出首板日至情绪结束日的高度；待确认表示尚未出现连续 4 日不创新高；数据不足表示缺少涨停状态或日线，必须标出原因。</div>__EMOTION_SECTION__</section>
<section class="panel" id="method"><h2>方法、数据限制与审计</h2><details open><summary>核心指标口径</summary><p class="muted">对每个目标交易日 t，仅纳入沪市主板、深市主板、科创板和创业板；先剔除ST、*ST、退市、退市整理及上市不足__NEW_DAYS__个自然日的新股，再以复权收盘价计算 adjusted_close(t) / adjusted_close(t-10) - 1。分别提取沪深主板、科创成长板和两组合并后的总体最大值。该指标描述短线情绪高度，不预测未来收益，也不是独立交易信号。</p></details><details><summary>数据质量与限制</summary><div class="callout">__WARNING__ 历史ST覆盖状态为“__ST_LABEL__”。最大值容易受到独立事件、连续一字板及不同涨跌幅制度影响，需结合前排结构与分市场结果阅读。</div></details><details><summary>逻辑数据源</summary><ul class="source-list">__SOURCES__</ul></details><details><summary>资格过滤审计（__AUDIT_COUNT__条）</summary><div class="table-wrap"><table><thead><tr><th>日期</th><th>名称</th><th>代码</th><th>10日涨幅</th><th>排除原因</th></tr></thead><tbody>__AUDIT_ROWS__</tbody></table></div></details><details><summary>实际处理链路</summary><div class="table-wrap"><table><thead><tr><th>步骤</th><th>组件</th><th>目的</th><th>状态</th></tr></thead><tbody>__PROCESS_ROWS__</tbody></table></div></details><p class="disclaimer">本报告基于公开数据与确定性量化计算，仅供研究参考，不构成投资建议。短线情绪高度可能钝化、失真或受个别股票影响，历史表现不预示未来收益。</p></section></main></body></html>'''
    replacements = {
        "__TITLE__": esc(data.get("title") or "短线情绪高度"),
        "__SUBTITLE__": esc(data.get("subtitle") or "基于沪深主板与科创成长板每日10日涨幅最大值，观察短线情绪高度的扩张、分化与收缩"),
        "__LATEST__": esc(latest.get("date", "—")), "__DATE_RANGE__": esc(range_text), "__NEW_DAYS__": str(data.get("new_stock_threshold_days", 60)),
        "__THRESHOLD__": f"{threshold:g}", "__TLDR__": tldr, "__STATE__": esc(state), "__LATEST_MAX__": fmt_pct(latest.get("maximum")),
        "__LATEST_CHANGE__": fmt_pct(latest_change), "__CHANGE_CLASS__": "up" if latest.get("direction", 0) > 0 else "down" if latest.get("direction", 0) < 0 else "muted",
        "__LATEST_LEADER__": esc(latest.get("leader_name", "—")), "__LATEST_CODE__": esc(latest.get("leader_code", "—")), "__LATEST_MARKET__": esc(latest.get("leader_market", "—")),
        "__LOW__": fmt_pct(low.get("maximum")), "__PEAK__": fmt_pct(peak.get("maximum")), "__SWITCHES__": str(leader_switches),
        "__VALID_CLASS__": "good" if validation_ok else "warn", "__VALID__": "校验通过" if validation_ok else "待复核",
        "__ST_CLASS__": "good" if st_coverage == "complete" else "warn", "__ST_LABEL__": "完整" if st_coverage == "complete" else "部分降级",
        "__COVERAGE__": esc(coverage), "__FAILED__": str(effective_failed), "__FAIL_CLASS__": "good" if effective_failed == 0 else "warn",
        "__OVERALL_EMOTION_CHART__": emotion_svg(metrics, "全市"),
        "__MAINBOARD_EMOTION_CHART__": emotion_svg(metrics, "沪深主板"),
        "__GROWTH_EMOTION_CHART__": emotion_svg(metrics, "科创成长板"),
        "__POOL_TREND_META__": pool_trend_meta_html(pool, threshold),
        "__POOL_TREND_CHART__": pool_trend_svg(pool),
        "__STRUCTURE_SECTION__": structure_section_html(structure_rows),
        "__EMOTION_SECTION__": emotion_section_html(emotion_rows),
        "__TREND_ROWS__": trend_rows, "__BREADTH_CHART__": breadth_svg(metrics, threshold),
        "__MARKET_HEADER__": market_header, "__MARKET_ROWS__": market_rows, "__LATEST_TOP__": latest_top,
        "__WARNING__": esc(data.get("warning", "无额外数据质量警告。")), "__SOURCES__": source_html, "__AUDIT_COUNT__": str(len(audit_excluded)),
        "__AUDIT_ROWS__": audit_html, "__PROCESS_ROWS__": process_html,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def main() -> int:
    parser = argparse.ArgumentParser(description="Build daily maximum 10-day return emotion-height HTML report")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--forward-daily", type=Path, default=None, help="前复权日线目录，用于达标池三口径趋势")
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    forward_close = load_forward_close(args.forward_daily)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(data, forward_close), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "dates": len(data.get("dates", [])), "core_metric": "daily_maximum_10d_return", "forward_daily_symbols": len(forward_close)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
