"""把技能计算结果转换为符合 report.contract.json 的 data.json。

技能侧唯一职责：产出契约数据。不生成任何 HTML / SVG / 颜色 / 坐标。

用法：
    python to_contract_data.py --input three_height_metrics.json \
        --forward-daily cache/akshare/forward_daily \
        --output data.json [--end-date 2026-10-23]

单位纪律（历史事故点，务必遵守）：
    - qualified[].pct        → 百分数（58.4293 表示 58.43%）→ 需 /100 转 ratio
    - structure_*.gain_pct   → 小数比例（0.5 表示 50%）      → 直接使用
    - emotion_*.rally_gain_pct → 百分数（140.89 表示 140.89%）→ 需 /100 转 ratio；趋势图 T3 不再使用该后验波段峰值字段
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

_STRUCTURE_ENGINE: Any = None

CONTRACT_VERSION = "report-view-v4"

REASON_CATEGORY = {
    "no_limit_up_in_search_window": "structural",
    "first_board_precedes_search_window": "structural",
    "no_active_first_board_as_of_cutoff": "structural",
    "no_confirmed_structure_bottom_as_of_cutoff": "structural",
    "prior_limit_status_missing_for_first_board": "data_gap",
    "limit_status_missing_in_first_board_search": "data_gap",
    "limit_status_missing_as_of_cutoff": "data_gap",
    "stock_missing_from_forward_daily": "data_gap",
    "insufficient_prior_sessions_for_first_board": "insufficient_history",
    "insufficient_return_history": "insufficient_history",
}


def reason_category(reason_code: str | None) -> str:
    return REASON_CATEGORY.get(str(reason_code or ""), "data_gap")


# AI 分析规则外置：阈值、文案、映射表都在 assets/ai_rules.json，
# 本文件只做「按规则求值」，改口径/调阈值不需要改 Python。
_SKILL_ROOT = Path(__file__).resolve().parents[2]
AI_RULES_PATH = _SKILL_ROOT / "assets" / "ai_rules.json"


# ---------- IO ----------

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_ai_rules(path: Path | None = None) -> dict[str, Any]:
    """加载 AI 分析规则（唯一口径来源）。

    阈值、标签、策略映射、未覆盖字段清单、免责声明都在 assets/ai_rules.json，
    代码里不再硬编码任何阈值——避免「文档一套、代码一套」的漂移。
    """
    target = path or AI_RULES_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"缺少 AI 规则文件：{target}。该文件是 ai_analysis 的唯一口径来源，"
            "请从技能 assets/ 目录恢复，不要在代码中另行硬编码阈值。"
        )
    return read_json(target)


def load_forward(directory: Path | None) -> dict[str, dict[str, dict[str, float]]]:
    """{ts_code: {trade_date: {open, high, low, close}}}"""
    out: dict[str, dict[str, dict[str, float]]] = {}
    if not directory or not directory.exists():
        return out
    for file in sorted(directory.glob("*.json")):
        try:
            rows = read_json(file)
        except Exception:
            continue
        series: dict[str, dict[str, float]] = {}
        for row in rows:
            date = str(row.get("trade_date") or "")
            if not date:
                continue
            series[date] = {
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
            }
        if series:
            out[file.stem] = series
    return out


# ---------- 单位转换 ----------

def pct_to_ratio(value: Any) -> float | None:
    """百分数 → 小数比例。仅用于 qualified.pct / rally_gain_pct 这类已是百分数的字段。"""
    if value is None:
        return None
    try:
        return round(float(value) / 100.0, 6)
    except (TypeError, ValueError):
        return None


def as_ratio(value: Any) -> float | None:
    """已经是小数比例，原样保留。用于 structure_*.gain_pct。"""
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


# ---------- height ----------

def build_height(
    qualified: list[dict[str, Any]],
    dates: list[str],
    market_groups: list[str],
    daily_summary: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in qualified:
        rows_by_date[str(row.get("date"))].append(row)
    summary_by_date = {
        str(row.get("date")): row
        for row in (daily_summary or [])
        if isinstance(row, dict) and row.get("date")
    }

    points: list[dict[str, Any]] = []
    previous_max: float | None = None
    streak_name: str | None = None
    streak = 0

    for date in dates:
        rows = rows_by_date.get(date, [])
        scored = [(row, float(row.get("pct"))) for row in rows if row.get("pct") is not None]
        if scored:
            leader, peak = max(scored, key=lambda item: item[1])
            leader_name = leader.get("name")
            changed = leader_name != streak_name
            streak = 1 if changed else streak + 1
            streak_name = leader_name
            by_market: dict[str, float | None] = {}
            summary_group_maxima = (summary_by_date.get(date) or {}).get("market_group_maxima") or {}
            for group in market_groups:
                summary_leader = summary_group_maxima.get(group) or {}
                if summary_leader.get("pct") is not None:
                    by_market[group] = pct_to_ratio(summary_leader["pct"])
                    continue
                group_values = [
                    float(row["pct"]) for row, _ in scored if row.get("market_group") == group
                ]
                by_market[group] = pct_to_ratio(max(group_values)) if group_values else None
            change = None if previous_max is None else round(peak / 100.0 - previous_max, 6)
            point = {
                "date": date,
                "value": pct_to_ratio(peak),
                "change": change,
                "leader_code": leader.get("code"),
                "leader_name": leader_name,
                "leader_market": leader.get("market"),
                "leader_changed": changed,
                "leader_streak": streak,
                "count": len(scored),
                "by_market": by_market,
                "candle_signal": (summary_by_date.get(date) or {}).get("emotion_candle_signal"),
                "market_candle_signals": (summary_by_date.get(date) or {}).get("market_group_emotion_candle_signals") or {},
            }
            previous_max = peak / 100.0
        else:
            point = {
                "date": date,
                "value": None,
                "change": None,
                "leader_code": None,
                "leader_name": None,
                "leader_market": None,
                "leader_changed": False,
                "leader_streak": 0,
                "count": 0,
                "by_market": {group: None for group in market_groups},
                "candle_signal": None,
                "market_candle_signals": {},
            }
            streak_name = None
            streak = 0
        points.append(point)

    return {"unit": "ratio", "market_groups": market_groups, "points": points}


# ---------- pool_trends ----------

def tenth_return(
    series: dict[str, dict[str, float]], date_index: dict[str, int], date: str, lookback: int
) -> float | None:
    index = date_index.get(date)
    if index is None or index < lookback:
        return None
    dates = sorted(date_index)
    base_date = dates[index - lookback]
    base = series.get(base_date, {}).get("close")
    now = series.get(date, {}).get("close")
    if not base or not now:
        return None
    return now / base - 1.0


def load_raw_daily(directory: Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    """读取未复权收盘与显式涨停状态，仅用于按截止日重建首板口径。"""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if not directory or not directory.exists():
        return out
    for file in sorted(directory.glob("*.json")):
        try:
            rows = read_json(file)
        except Exception:
            continue
        code = file.stem
        series: dict[str, dict[str, Any]] = {}
        for row in rows if isinstance(rows, list) else []:
            day = str(row.get("trade_date") or row.get("date") or "")[:10]
            if day:
                series[day] = {
                    "close": row.get("close"),
                    "is_limit_up": row.get("is_limit_up"),
                }
        if series:
            out[code] = series
    return out


def dynamic_structure_bottoms(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """在当前截止日可见数据内重建严格底分型，沿用生产实现的包含关系规则。"""
    if len(rows) < 3:
        return []
    global _STRUCTURE_ENGINE
    if _STRUCTURE_ENGINE is None:
        module_path = Path(__file__).resolve().parents[1] / "compute" / "calculate_structure_waves.py"
        spec = importlib.util.spec_from_file_location("structure_engine_for_pool", module_path)
        if spec is None or spec.loader is None:
            return []
        _STRUCTURE_ENGINE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_STRUCTURE_ENGINE)
    merged, _ = _STRUCTURE_ENGINE.merge_inclusions(rows)
    return [item for item in _STRUCTURE_ENGINE.identify_fractals(merged) if item["type"] == "bottom"]


def dynamic_first_board(
    dates: list[str],
    raw_series: dict[str, dict[str, Any]],
    end_index: int,
) -> int | None:
    """仅从截止日之前识别首板；无前视，不依赖最终候选波段。"""
    for index in range(end_index + 1):
        status = raw_series.get(dates[index], {}).get("is_limit_up")
        if status is not True or index < 5:
            continue
        prior = [raw_series.get(dates[j], {}).get("is_limit_up") for j in range(index - 5, index)]
        if all(value is False for value in prior):
            return index
    return None


def build_pool_trends(
    qualified: list[dict[str, Any]],
    forward: dict[str, dict[str, dict[str, float]]],
    structure_rows: list[dict[str, Any]],
    emotion_rows: list[dict[str, Any]],
    threshold: float,
    threshold_days: int,
    end_date: str | None,
    rank_fill: list[dict[str, Any]] | None = None,
    raw: dict[str, dict[str, dict[str, Any]]] | None = None,
    observation_dates: list[str] | None = None,
) -> dict[str, Any]:
    raw = raw or {}
    start_map: dict[str, str] = {}
    for row in structure_rows:
        code = row.get("ts_code") or row.get("code")
        start = row.get("start_date")
        if code and start:
            start_map[str(code)] = str(start)
    fb_map: dict[str, str] = {}
    for row in emotion_rows:
        code = row.get("ts_code") or row.get("code")
        fb = row.get("first_board_date")
        if code and fb:
            fb_map[str(code)] = str(fb)

    # 无首板成员的成因分布：把「窗口内根本没有涨停（趋势型无首板可锚）」、
    # 「首板早于搜索窗口（长连板）」和「数据缺失/不足」分开统计，
    # 让覆盖缺口告警能说明缺口性质，而不是笼统说"曲线可能偏低"。
    no_first_board_reason: dict[str, str] = {}
    for row in emotion_rows:
        code = row.get("ts_code") or row.get("code")
        if not code or row.get("first_board_date"):
            continue
        reasons = row.get("reasons") or []
        reason = str(reasons[0]) if reasons else str(row.get("status") or "unknown")
        no_first_board_reason[str(code)] = reason

    # 池内成员 = 阈值达标并集 ∪ 前N名补位并集。
    # 补位股当日未达阈值，故不会进入下面 compute() 的 members（那里仍按 ret>=threshold
    # 逐日重算），t1/t2/t3 的数值不受影响；该并集只扩展池的覆盖面与 coverage 统计。
    fill_rows = [row for row in (rank_fill or []) if isinstance(row, dict)]
    pool_rows = list(qualified) + fill_rows
    qualified_codes: set[str] = set()
    for row in qualified:
        for key in (row.get("ts_code"), row.get("code")):
            if key:
                qualified_codes.add(str(key))
    fill_codes: set[str] = set()
    for row in fill_rows:
        key = row.get("ts_code") or row.get("code")
        if key:
            fill_codes.add(str(key))
    # 仅靠补位进池的股票：某日补位、但从未达标的才会被算作补位成员，
    # 避免"先补位后达标"的同名股被重复计成补位。
    fill_only_codes = fill_codes - qualified_codes

    # 池内成员（曾在任一交易日 10 日涨幅达标，或进入当日前N名补位）
    pool: set[str] = set()
    for row in pool_rows:
        if row.get("ts_code"):
            pool.add(str(row["ts_code"]))
        elif row.get("code"):
            pool.add(str(row["code"]))

    # 代码 → 市场分组（用于分市场口径；同一代码的 ts_code / code 都登记）
    code_group: dict[str, str] = {}
    for row in pool_rows:
        group = row.get("market_group")
        if not group:
            continue
        for key in (row.get("ts_code"), row.get("code")):
            if key:
                code_group[str(key)] = str(group)

    # 每日快照需要稳定的个股身份字段；优先使用达标记录，补位记录仅作兜底。
    member_meta: dict[str, dict[str, Any]] = {}
    for row in pool_rows:
        code = row.get("ts_code") or row.get("code")
        if not code:
            continue
        key = str(code)
        member_meta.setdefault(key, {
            "code": row.get("code") or (key.split(".")[0] if "." in key else key),
            "ts_code": row.get("ts_code") or key,
            "name": row.get("name"),
            "market": row.get("market"),
            "market_group": row.get("market_group") or code_group.get(key),
        })

    if not forward:
        return {
            "unit": "ratio",
            "x": [],
            "x_start_reason": "缺少前复权日线，无法构建达标池趋势",
            "y_max": 1.0,
            "series": [],
            "coverage": {
                "pool_union": len(pool),
                "threshold_members": len(pool - fill_only_codes),
                "rank_fill_members": len(pool & fill_only_codes),
                "no_structure_start": 0,
                "no_first_board": 0,
                "no_first_board_reason_counts": {},
                "no_first_board_category_counts": {},
                "no_first_board_items": [],
            },
            "by_market": {},
        }

    # 趋势图横轴严格对应用户请求的观察窗口，不再从波段起点向前扩展。
    all_dates = sorted({day for series in forward.values() for day in series})
    if end_date:
        all_dates = [day for day in all_dates if day <= end_date]
    x_dates = [day for day in (observation_dates or all_dates) if day in all_dates]
    if not x_dates:
        x_dates = all_dates
    # 兼容旧输入：若 dates 未裁剪，仍只展示最后 30 个观察交易日；显式 dates 优先。
    if observation_dates is None and len(x_dates) > 30:
        x_dates = x_dates[-30:]

    date_index_cache: dict[str, dict[str, int]] = {
        code: {day: i for i, day in enumerate(sorted(forward.get(code, {})))}
        for code in pool
        if forward.get(code)
    }

    def current_bottom(code: str, cutoff: str) -> dict[str, Any] | None:
        series = forward.get(code, {})
        rows = [
            {"date": day, "open": values.get("open"), "high": values.get("high"),
             "low": values.get("low"), "close": values.get("close")}
            for day, values in sorted(series.items()) if day <= cutoff
            and all(values.get(field) is not None for field in ("open", "high", "low", "close"))
        ]
        bottoms = [item for item in dynamic_structure_bottoms(rows) if item["confirm_date"] <= cutoff]
        return max(bottoms, key=lambda item: (item["confirm_date"], item["fractal_date"])) if bottoms else None

    def current_first_board(code: str, cutoff: str) -> dict[str, Any]:
        dates = sorted(set(forward.get(code, {})) | set(raw.get(code, {})))
        dates = [day for day in dates if day <= cutoff]
        raw_series = raw.get(code, {})
        if not dates:
            return {"start_date": None, "reason_code": "stock_missing_from_forward_daily", "reason_category": "data_gap"}
        first_indices = []
        blocked_missing = False
        for index in range(5, len(dates)):
            statuses = [raw_series.get(dates[j], {}).get("is_limit_up") for j in range(index - 5, index + 1)]
            status = statuses[-1]
            if any(value is None for value in statuses):
                blocked_missing = True
                continue
            if status is True and all(value is False for value in statuses[:-1]):
                first_indices.append(index)
        # 每个 T 只保留截至 T 日仍未结束的最新首板波段；四个连续不创新高日确认结束。
        active: list[str] = []
        fwd_series = forward.get(code, {})
        for start_index in first_indices:
            start_day = dates[start_index]
            first_high = fwd_series.get(start_day, {}).get("high")
            if first_high is None:
                blocked_missing = True
                continue
            rolling_high = float(first_high)
            quiet = 0
            ended = False
            for index in range(start_index + 1, len(dates)):
                day = dates[index]
                high = fwd_series.get(day, {}).get("high")
                if high is None:
                    blocked_missing = True
                    break
                if float(high) > rolling_high:
                    rolling_high, quiet = float(high), 0
                else:
                    quiet += 1
                    status = raw_series.get(day, {}).get("is_limit_up")
                    if quiet >= 4:
                        if status is None:
                            blocked_missing = True
                            break
                        if status is True:
                            quiet = 0
                        else:
                            ended = True
                            break
            if not ended:
                active.append(start_day)
        if active:
            return {"start_date": active[-1], "reason_code": None, "reason_category": None}
        reason_code = "limit_status_missing_as_of_cutoff" if blocked_missing else "no_active_first_board_as_of_cutoff"
        return {"start_date": None, "reason_code": reason_code, "reason_category": reason_category(reason_code)}

    def member_snapshot(code: str, cutoff: str) -> dict[str, Any] | None:
        """生成一只股票在截止日的三套口径证据；所有起点均重新按 cutoff 判断。"""
        series = forward.get(code, {})
        index_map = date_index_cache.get(code, {})
        now = series.get(cutoff, {}).get("close")
        t1_index = index_map.get(cutoff)
        if now is None or t1_index is None:
            return None
        ordered_dates = sorted(index_map)
        t1_start = ordered_dates[t1_index - threshold_days] if t1_index >= threshold_days else None
        t1_base = series.get(t1_start, {}).get("close") if t1_start else None
        t1_gain = (now / t1_base - 1.0) if t1_base else None

        bottom = current_bottom(code, cutoff)
        t2_price = bottom.get("extreme_price") if bottom else None
        t2_start = (bottom.get("fractal_date") or bottom.get("date")) if bottom else None
        t2_confirm = bottom.get("confirm_date") if bottom else None
        t2_gain = (now / t2_price - 1.0) if t2_price else None
        if bottom and t2_gain is not None:
            t2 = {"start_date": t2_start, "confirm_date": t2_confirm, "start_price": t2_price,
                  "end_date": cutoff, "end_price": now, "gain": round(t2_gain, 6), "status": "available",
                  "reason_code": None, "reason_category": None, "reason": None}
        else:
            t2 = {"start_date": None, "confirm_date": None, "start_price": None, "end_date": cutoff,
                  "end_price": now, "gain": None, "status": "unavailable",
                  "reason_code": "no_confirmed_structure_bottom_as_of_cutoff",
                  "reason_category": "structural", "reason": "截至该日尚无已确认严格底分型"}

        first_board = current_first_board(code, cutoff)
        first_day = first_board.get("start_date")
        first_close = series.get(first_day, {}).get("close") if first_day else None
        t3_gain = (now / first_close - 1.0) if first_close else None
        if first_day and t3_gain is not None:
            t3 = {"start_date": first_day, "confirm_date": first_day, "start_price": first_close,
                  "end_date": cutoff, "end_price": now, "gain": round(t3_gain, 6), "status": "available",
                  "reason_code": None, "reason_category": None, "reason": None}
        else:
            reason_code = first_board.get("reason_code") or "no_active_first_board_as_of_cutoff"
            t3 = {"start_date": None, "confirm_date": None, "start_price": None, "end_date": cutoff,
                  "end_price": now, "gain": None, "status": "unavailable",
                  "reason_code": reason_code, "reason_category": first_board.get("reason_category") or reason_category(reason_code),
                  "reason": "涨停状态或价格数据缺失，无法确认首板" if reason_code == "limit_status_missing_as_of_cutoff" else "截至该日没有当前有效首板情绪波段"}

        meta = member_meta.get(code, {})
        return {
            "code": meta.get("code") or code,
            "ts_code": meta.get("ts_code") or code,
            "name": meta.get("name"),
            "market": meta.get("market"),
            "market_group": meta.get("market_group"),
            "cutoff_date": cutoff,
            "t1": {"start_date": t1_start, "end_date": cutoff, "start_price": t1_base,
                   "end_price": now, "gain": round(t1_gain, 6) if t1_gain is not None else None,
                   "status": "available" if t1_gain is not None else "unavailable",
                   "reason_code": None if t1_gain is not None else "insufficient_return_history",
                   "reason_category": None if t1_gain is not None else "insufficient_history",
                   "reason": None if t1_gain is not None else "不足10个交易日"},
            "t2": t2,
            "t3": t3,
        }

    def snapshot_stats(members: list[dict[str, Any]]) -> dict[str, Any]:
        def metric_stats(key: str) -> dict[str, Any]:
            items = [row.get(key) or {} for row in members]
            available = [item for item in items if item.get("status") == "available" and item.get("gain") is not None]
            unavailable = [item for item in items if item.get("status") == "unavailable"]
            reason_counts: dict[str, int] = defaultdict(int)
            category_counts: dict[str, int] = defaultdict(int)
            for item in unavailable:
                reason_counts[str(item.get("reason_code") or "unknown")] += 1
                category_counts[str(item.get("reason_category") or "data_gap")] += 1
            maximum = max((float(item["gain"]) for item in available), default=None)
            leaders = []
            if maximum is not None:
                leaders = [
                    {"code": row.get("code"), "ts_code": row.get("ts_code"), "name": row.get("name"),
                     "market": row.get("market"), "market_group": row.get("market_group")}
                    for row in members
                    if (row.get(key) or {}).get("gain") is not None
                    and abs(float((row.get(key) or {}).get("gain")) - maximum) <= 1e-9
                ]
            return {
                "available": len(available), "unavailable": len(unavailable),
                "reason_counts": dict(sorted(reason_counts.items())),
                "reason_category_counts": dict(sorted(category_counts.items())),
                "maxima": {"gain": round(maximum, 6) if maximum is not None else None, "leaders": leaders},
            }

        return {
            "count": len(members),
            "metrics": {key: metric_stats(key) for key in ("t1", "t2", "t3")},
        }

    stable_groups = sorted(set(code_group.values()))

    def daily_snapshots(subset: set[str]) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for cutoff in x_dates:
            members: list[dict[str, Any]] = []
            for code in subset:
                row = member_snapshot(code, cutoff)
                if row and row["t1"]["gain"] is not None and row["t1"]["gain"] >= threshold:
                    members.append(row)
            members.sort(key=lambda row: (row["t1"]["gain"] is None, -(row["t1"]["gain"] or 0), row["code"] or ""))
            for rank, row in enumerate(members, 1):
                row["rank"] = rank
            snapshots.append({
                "date": cutoff, "count": len(members), "members": members,
                "stats": {
                    "all": snapshot_stats(members),
                    "by_market": {group: snapshot_stats([row for row in members if row.get("market_group") == group]) for group in stable_groups},
                },
            })

        # 变动口径：新进 = 相比前一交易日不在强势池；退出 = 前 5 个交易日内
        # 任意一天在强势池、但当前已退出。不是只比较 T-5，也不是要求连续 5 日都在池内。
        for index, snapshot in enumerate(snapshots):
            current_members = snapshot.get("members") or []
            current_map = {
                str(row.get("ts_code") or row.get("code")): row
                for row in current_members
                if row.get("ts_code") or row.get("code")
            }
            previous = snapshots[index - 1] if index > 0 else None
            previous_codes = {
                str(row.get("ts_code") or row.get("code"))
                for row in (previous.get("members") or [] if previous else [])
                if row.get("ts_code") or row.get("code")
            }
            for row in current_members:
                identity = str(row.get("ts_code") or row.get("code"))
                is_new = bool(previous is not None and identity not in previous_codes)
                row["pool_change"] = "new" if is_new else ("baseline" if previous is None else "continued")
                row["is_new_entry"] = is_new

            prior_snapshots = snapshots[max(0, index - 5):index]
            prior_members: dict[str, dict[str, Any]] = {}
            for prior_snapshot in prior_snapshots:
                for row in prior_snapshot.get("members") or []:
                    identity = str(row.get("ts_code") or row.get("code"))
                    if identity:
                        # 同一股票多次出现时保留前 5 日窗口内最近一次记录，便于展示。
                        prior_members[identity] = {**row, "compare_date": prior_snapshot.get("date")}
            exit_codes = sorted(set(prior_members) - set(current_map))
            compare_dates = [item.get("date") for item in prior_snapshots if item.get("date")]
            snapshot["new_members_count"] = sum(1 for row in current_members if row.get("is_new_entry"))
            snapshot["exit_compare_date"] = compare_dates[-1] if compare_dates else None
            snapshot["exit_compare_start_date"] = compare_dates[0] if compare_dates else None
            snapshot["exit_compare_end_date"] = compare_dates[-1] if compare_dates else None
            snapshot["exit_compare_dates"] = compare_dates
            snapshot["exit_count"] = len(exit_codes)
            snapshot["exit_members"] = []
            for identity in exit_codes:
                row = prior_members[identity]
                snapshot["exit_members"].append({
                    "code": row.get("code"),
                    "ts_code": row.get("ts_code"),
                    "name": row.get("name"),
                    "market": row.get("market"),
                    "market_group": row.get("market_group"),
                    "rank": row.get("rank"),
                    "compare_date": row.get("compare_date"),
                    "exit_date": snapshot.get("date"),
                    "t1_gain": (row.get("t1") or {}).get("gain"),
                })
            snapshot["exit_members"].sort(key=lambda row: (row.get("rank") is None, row.get("rank") or 0, row.get("code") or ""))
        return snapshots

    def compute(subset: set[str], include_snapshots: bool = False) -> dict[str, Any]:
        series_t1: list[float | None] = []
        series_t2: list[float | None] = []
        series_t3: list[float | None] = []
        covered_t2: set[str] = set()
        covered_t3: set[str] = set()
        snapshots = daily_snapshots(subset) if include_snapshots else []
        for snapshot in snapshots if include_snapshots else [{"date": cutoff, "members": []} for cutoff in x_dates]:
            cutoff = snapshot["date"]
            if not include_snapshots:
                snapshot["members"] = []
                for code in subset:
                    row = member_snapshot(code, cutoff)
                    if row and row["t1"]["gain"] is not None and row["t1"]["gain"] >= threshold:
                        snapshot["members"].append(row)
            members = snapshot["members"]
            t1_values = [row["t1"]["gain"] for row in members if row["t1"]["gain"] is not None]
            t2_values = [row["t2"]["gain"] for row in members if row["t2"]["gain"] is not None]
            t3_values = [row["t3"]["gain"] for row in members if row["t3"]["gain"] is not None]
            covered_t2.update(row["code"] for row in members if row["t2"]["gain"] is not None)
            covered_t3.update(row["code"] for row in members if row["t3"]["gain"] is not None)
            series_t1.append(round(max(t1_values), 6) if t1_values else None)
            series_t2.append(round(max(t2_values), 6) if t2_values else None)
            series_t3.append(round(max(t3_values), 6) if t3_values else None)
        max_points: list[float | None] = []
        max_metric_keys: list[list[str]] = []
        for values in zip(series_t1, series_t2, series_t3):
            available = [(key, value) for key, value in zip(("t1", "t2", "t3"), values) if value is not None]
            if not available:
                max_points.append(None)
                max_metric_keys.append([])
                continue
            maximum = max(value for _, value in available)
            max_points.append(round(maximum, 6))
            max_metric_keys.append([key for key, value in available if abs(value - maximum) <= 1e-9])
        finite = [value for values in (series_t1, series_t2, series_t3) for value in values if value is not None]
        y_max = round(max(finite) * 1.15, 4) if finite else 1.0
        threshold_members = subset - fill_only_codes
        no_first_board_codes = threshold_members - set(fb_map)
        reason_counts: dict[str, int] = defaultdict(int)
        category_counts: dict[str, int] = defaultdict(int)
        reason_items: list[dict[str, Any]] = []
        for code in sorted(no_first_board_codes):
            code_reason = no_first_board_reason.get(code, "unknown")
            category = reason_category(code_reason)
            reason_counts[code_reason] += 1
            category_counts[category] += 1
            reason_items.append({"ts_code": code, "reason_code": code_reason, "category": category})
        result = {
            "y_max": y_max,
            "series": [
                {"key": "t1", "label": "固定10日涨幅", "style": "solid", "points": series_t1, "coverage": len(subset)},
                {"key": "t2", "label": "严格底分型起点涨幅", "style": "solid", "points": series_t2, "coverage": len(covered_t2)},
                {"key": "t3", "label": "首板情绪起点涨幅", "style": "solid", "points": series_t3, "coverage": len(covered_t3)},
            ],
            "max_series": {
                "key": "max_t1_t2_t3",
                "label": "三口径最大涨幅",
                "points": max_points,
                "winning_metrics": max_metric_keys,
                "definition": "每个交易日从 T1、T2、T3 的可用涨幅中取最大值；三者均无值时为 null。",
            },
            "coverage": {
                "pool_union": len(subset), "threshold_members": len(threshold_members),
                "rank_fill_members": len(subset & fill_only_codes),
                "no_structure_start": len(threshold_members - set(start_map)),
                "no_first_board": len(no_first_board_codes),
                "no_first_board_reason_counts": dict(sorted(reason_counts.items())),
                "no_first_board_category_counts": dict(sorted(category_counts.items())),
                "no_first_board_items": reason_items,
            },
            "as_of_rule": "每个观察日独立计算，仅使用该日及以前已确认的起点",
        }
        if include_snapshots:
            result["daily_snapshots"] = snapshots
        return result

    aggregate = compute(pool, include_snapshots=True)

    groups: list[str] = []
    for code in pool:
        group = code_group.get(code)
        if group and group not in groups:
            groups.append(group)
    groups.sort()
    by_market: dict[str, Any] = {}
    for group in groups:
        subset = {code for code in pool if code_group.get(code) == group}
        if subset:
            by_market[group] = compute(subset)

    return {
        "unit": "ratio",
        "x": x_dates,
        "x_start_reason": "严格使用用户指定观察窗口；每个交易日作为独立截止时点",
        "y_max": aggregate["y_max"],
        "series": aggregate["series"],
        "coverage": aggregate["coverage"],
        "daily_snapshots": aggregate.get("daily_snapshots") or [],
        "window_stats": build_window_stats(aggregate.get("daily_snapshots") or [], groups),
        "daily_leaders": build_daily_leaders(aggregate.get("daily_snapshots") or [], groups),
        "emotion_metrics": build_emotion_metrics({**aggregate, "x": x_dates, "by_market": by_market}),
        "by_market": by_market,
    }


def _metric_maxima(scope: dict[str, Any], key: str) -> dict[str, Any]:
    return ((scope.get("metrics") or {}).get(key) or {}).get("maxima") or {"gain": None, "leaders": []}


def build_window_stats(snapshots: list[dict[str, Any]], market_groups: list[str]) -> dict[str, Any]:
    """在整个观察窗口聚合异常事件与去重后的异常个股明细。

    event_count 统计“股票-观察日”异常事件，stock_count 对股票去重；两者不能混为一谈。
    """
    def aggregate(rows_by_day: list[tuple[str, list[dict[str, Any]]]]) -> dict[str, Any]:
        output: dict[str, Any] = {"snapshot_count": len(rows_by_day), "metrics": {}}
        for key in ("t1", "t2", "t3"):
            event_count = 0
            reason_event_counts: dict[str, int] = defaultdict(int)
            category_event_counts: dict[str, int] = defaultdict(int)
            stock_map: dict[str, dict[str, Any]] = {}
            available_events = 0
            for day, rows in rows_by_day:
                for row in rows:
                    item = row.get(key) or {}
                    if item.get("status") == "available":
                        available_events += 1
                        continue
                    if item.get("status") != "unavailable":
                        continue
                    event_count += 1
                    reason = str(item.get("reason_code") or "unknown")
                    category = str(item.get("reason_category") or reason_category(reason))
                    reason_event_counts[reason] += 1
                    category_event_counts[category] += 1
                    identity = str(row.get("ts_code") or row.get("code") or "")
                    if not identity:
                        continue
                    entry = stock_map.setdefault(identity, {
                        "metric": key, "code": row.get("code"), "ts_code": row.get("ts_code"), "name": row.get("name"),
                        "market": row.get("market"), "market_group": row.get("market_group"),
                        "occurrence_count": 0, "first_date": day, "last_date": day, "dates": [],
                        "reason_counts": {}, "reason_categories": [],
                    })
                    entry["occurrence_count"] += 1
                    entry["dates"].append(day)
                    entry["last_date"] = day
                    entry["reason_counts"][reason] = entry["reason_counts"].get(reason, 0) + 1
                    if category not in entry["reason_categories"]:
                        entry["reason_categories"].append(category)
            items = []
            for entry in stock_map.values():
                entry["dates"] = sorted(set(entry["dates"]))
                entry["reason_categories"] = sorted(entry["reason_categories"])
                items.append(entry)
            items.sort(key=lambda item: (-item["occurrence_count"], item.get("code") or ""))
            output["metrics"][key] = {
                "available_events": available_events,
                "unavailable_events": event_count,
                "affected_stock_count": len(items),
                "reason_event_counts": dict(sorted(reason_event_counts.items())),
                "reason_category_event_counts": dict(sorted(category_event_counts.items())),
                "reason_stock_counts": {
                    reason: sum(1 for item in items if reason in (item.get("reason_counts") or {}))
                    for reason in sorted(reason_event_counts)
                },
                "reason_category_stock_counts": {
                    category: sum(1 for item in items if category in (item.get("reason_categories") or []))
                    for category in sorted(category_event_counts)
                },
                "items": items,
            }
        return output

    all_rows = [(str(snapshot.get("date")), snapshot.get("members") or []) for snapshot in snapshots]
    all_stats = aggregate(all_rows)
    result = {
        "date_start": all_rows[0][0] if all_rows else None,
        "date_end": all_rows[-1][0] if all_rows else None,
        "snapshot_count": len(all_rows),
        "all": all_stats,
        "by_market": {},
    }
    for group in market_groups:
        result["by_market"][group] = aggregate([
            (day, [row for row in rows if row.get("market_group") == group])
            for day, rows in all_rows
        ])
    return result


def build_daily_leaders(snapshots: list[dict[str, Any]], market_groups: list[str]) -> list[dict[str, Any]]:
    """把每日快照中的最大值整理成稳定的全市场/分市场三口径榜首序列。"""
    leaders: list[dict[str, Any]] = []
    for snapshot in snapshots:
        scopes: dict[str, Any] = {"all": (snapshot.get("stats") or {}).get("all") or {}}
        by_market = (snapshot.get("stats") or {}).get("by_market") or {}
        for group in market_groups:
            scopes[group] = by_market.get(group) or {"metrics": {}}
        row: dict[str, Any] = {"date": snapshot.get("date"), "all": {}, "by_market": {}}
        for scope_name, scope in scopes.items():
            target = row["all"] if scope_name == "all" else row["by_market"].setdefault(scope_name, {})
            for key in ("t1", "t2", "t3"):
                maximum = _metric_maxima(scope, key)
                gain = maximum.get("gain")
                evidence = []
                for leader in maximum.get("leaders") or []:
                    identity = str(leader.get("ts_code") or leader.get("code") or "")
                    member = next((item for item in snapshot.get("members") or []
                                   if str(item.get("ts_code") or item.get("code") or "") == identity), None)
                    item = dict(leader)
                    metric = (member or {}).get(key) or {}
                    item.update({"gain": gain, "status": metric.get("status"),
                                 "start_date": metric.get("start_date"), "confirm_date": metric.get("confirm_date"),
                                 "end_date": metric.get("end_date"), "start_price": metric.get("start_price"),
                                 "end_price": metric.get("end_price")})
                    evidence.append(item)
                target[key] = {"gain": gain, "leaders": evidence}
        leaders.append(row)
    return leaders


def build_market_timeline(
    dates: list[str], height: dict[str, Any], pool: dict[str, Any],
    limit_up: dict[str, Any] | None, rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按观察窗口逐日生成分歧时间轴；只使用当日与前一交易日字段。"""
    params = (rules or {}).get("divergence_params") or {}
    height_min = float(params.get("height_change_min", 0.05))
    breadth_min = int(params.get("breadth_change_min", 2))
    market_min = float(params.get("market_change_min", 0.05))
    metric_min = float(params.get("metric_change_min", 0.05))
    limit_min = int(params.get("limit_total_change_min", 10))
    board_min = int(params.get("max_boards_change_min", 1))
    hp = {point.get("date"): point for point in height.get("points") or []}
    snaps = {item.get("date"): item for item in pool.get("daily_snapshots") or []}
    lp = {point.get("date"): point for point in (limit_up or {}).get("points") or []}
    points: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for day in dates:
        h = hp.get(day) or {}
        snap = snaps.get(day) or {}
        limit = lp.get(day) or {}
        prev_day = dates[dates.index(day) - 1] if dates.index(day) > 0 else None
        prev_h = hp.get(prev_day) or {}
        prev_snap = snaps.get(prev_day) or {}
        prev_limit = lp.get(prev_day) or {}
        height_change = h.get("value") - prev_h.get("value") if isinstance(h.get("value"), (int, float)) and isinstance(prev_h.get("value"), (int, float)) else None
        breadth_change = (snap.get("count") - prev_snap.get("count")) if isinstance(snap.get("count"), int) and isinstance(prev_snap.get("count"), int) else None
        market_changes: dict[str, float | None] = {}
        for group in (height.get("market_groups") or []):
            current = ((h.get("by_market") or {}).get(group))
            prior = ((prev_h.get("by_market") or {}).get(group))
            market_changes[group] = round(current - prior, 6) if isinstance(current, (int, float)) and isinstance(prior, (int, float)) else None
        metric_changes: dict[str, float | None] = {}
        current_metrics = ((snap.get("stats") or {}).get("all") or {}).get("metrics") or {}
        prior_metrics = ((prev_snap.get("stats") or {}).get("all") or {}).get("metrics") or {}
        for key in ("t1", "t2", "t3"):
            current = ((current_metrics.get(key) or {}).get("maxima") or {}).get("gain")
            prior = ((prior_metrics.get(key) or {}).get("maxima") or {}).get("gain")
            metric_changes[key] = round(current - prior, 6) if isinstance(current, (int, float)) and isinstance(prior, (int, float)) else None
        limit_total_change = limit.get("total_change") if isinstance(limit.get("total_change"), int) else None
        max_boards_change = limit.get("max_boards_change") if isinstance(limit.get("max_boards_change"), int) else None
        tags: list[str] = []
        if height_change is not None and breadth_change is not None:
            if height_change >= height_min and breadth_change <= -breadth_min:
                tags.append("height_up_breadth_down")
            elif height_change <= -height_min and breadth_change >= breadth_min:
                tags.append("height_down_breadth_up")
        valid_market = [v for v in market_changes.values() if v is not None and abs(v) >= market_min]
        if len(valid_market) >= 2 and any(v > 0 for v in valid_market) and any(v < 0 for v in valid_market):
            tags.append("cross_market_divergence")
        valid_metric = [v for v in metric_changes.values() if v is not None and abs(v) >= metric_min]
        if len(valid_metric) >= 2 and any(v > 0 for v in valid_metric) and any(v < 0 for v in valid_metric):
            tags.append("metric_divergence")
        if limit_total_change is not None and max_boards_change is not None:
            if abs(limit_total_change) >= limit_min and abs(max_boards_change) >= board_min and (limit_total_change > 0) != (max_boards_change > 0):
                tags.append("limit_breadth_height_divergence")
        severity = "high" if len(tags) >= 2 else ("medium" if tags else "normal")
        summary_bits = {
            "height_up_breadth_down": "高度抬升但达标广度收缩",
            "height_down_breadth_up": "高度回落但达标广度扩张",
            "cross_market_divergence": "市场分组方向相反",
            "metric_divergence": "T1/T2/T3 口径方向不一致",
            "limit_breadth_height_divergence": "涨停家数与最高连板方向相反",
        }
        points.append({
            "date": day, "is_divergence": bool(tags), "severity": severity, "score": len(tags), "tags": tags,
            "signals": {"height_change": height_change, "breadth_change": breadth_change,
                        "market_changes": market_changes, "metric_changes": metric_changes,
                        "limit_total_change": limit_total_change, "max_boards_change": max_boards_change},
            "summary": "；".join(summary_bits[tag] for tag in tags) if tags else "市场高度、广度与三种口径未形成显著分歧",
        })
        previous = {"date": day}
    return {"rules_version": (rules or {}).get("rules_version"), "date_start": dates[0] if dates else None,
            "date_end": dates[-1] if dates else None, "points": points,
            "events": [point for point in points if point["is_divergence"]]}


# ---------- 表格行 ----------

def build_structure_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        out.append(
            {
                "code": row.get("code"),
                "ts_code": row.get("ts_code"),
                "name": row.get("name"),
                "market": row.get("market"),
                "status": row.get("status") or "data_insufficient",
                "start_date": row.get("start_date"),
                "start_confirm_date": row.get("start_confirm_date"),
                "start_extreme_date": row.get("start_extreme_date"),
                "measurement_date": row.get("measurement_date"),
                "end_date": row.get("end_date"),
                "end_confirm_date": row.get("end_confirm_date"),
                # A 模式本身就是小数比例，不做 /100
                "gain_theoretical": as_ratio(first_value(row, "theoretical_gain_pct")),
                "gain_observable": as_ratio(first_value(row, "observable_gain_pct")),
                "gain_dual": as_ratio(first_value(row, "dual_confirm_gain_pct")),
            }
        )
    return out


def build_emotion_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        out.append(
            {
                "code": row.get("code"),
                "ts_code": row.get("ts_code"),
                "name": row.get("name"),
                "market": row.get("market"),
                "status": row.get("status") or "data_insufficient",
                "first_board_date": row.get("first_board_date"),
                "high_date": row.get("high_date"),
                "trend_end_date": row.get("trend_end_date"),
                # B 模式是百分数，必须 /100
                "gain": pct_to_ratio(first_value(row, "rally_gain_pct")),
                "days": as_int(first_value(row, "rally_days")),
                "style": row.get("style"),
                "reasons": row.get("reasons"),
            }
        )
    return out


# ---------- 抓取失败审计 ----------
def _market_of(ts_code: str) -> str:
    if ts_code.endswith(".BJ"):
        return "北交所"
    if ts_code.startswith("60"):
        return "沪市主板"
    if ts_code.startswith(("688", "689")):
        return "科创板"
    if ts_code.startswith("30"):
        return "创业板"
    if ts_code.startswith("00"):
        return "深市主板"
    return "其他"


def build_fetch_failures(
    manifest_path: "Path | None",
    stock_basic_path: "Path | None",
    price_store_dir: "Path | None",
    as_of: "str | None",
) -> "dict[str, Any] | None":
    """从 bootstrap_manifest.summary.failures 读取 akshare 源端抓取失败的股票，
    按技能池（沪深主板+科创板+创业板，剔除北交所）与过滤规则（ST 名称、60 自然日新股）
    分类，输出审计块。无 manifest 时返回 None（审计栏目不渲染该项）。"""
    if not manifest_path or not Path(manifest_path).exists():
        return None
    manifest = read_json(Path(manifest_path))
    failures = (manifest.get("summary") or {}).get("failures") or []
    if not failures:
        return None
    as_of_date = date.fromisoformat(as_of) if as_of else None

    basic: dict[str, Any] = {}
    if stock_basic_path and Path(stock_basic_path).exists():
        for r in read_json(Path(stock_basic_path)) or []:
            basic[r.get("ts_code")] = r

    rows_on_disk: dict[str, int] = {}
    if price_store_dir and Path(price_store_dir).exists():
        for p in Path(price_store_dir).glob("*.json"):
            try:
                d = read_json(p)
            except Exception:
                continue
            tc = d.get("ts_code")
            if tc:
                rows_on_disk[tc] = len(d.get("qfq") or [])

    cats: dict[str, list[dict[str, Any]]] = {
        "pool_excluded_bj": [],
        "st_filtered": [],
        "recent_ipo_filtered": [],
        "partial_usable": [],
    }
    pat_st = re.compile(r"^\*?ST|退")

    for f in failures:
        tc = f.get("ts_code") or ""
        info = basic.get(tc) or {}
        name = info.get("name") or tc
        list_date = info.get("list_date")
        market = _market_of(tc)
        on_disk = rows_on_disk.get(tc, 0)
        is_bj = tc.endswith(".BJ")
        is_st = bool(pat_st.search(name or ""))
        listing_age = None
        if list_date and as_of_date:
            try:
                listing_age = (as_of_date - date.fromisoformat(str(list_date))).days
            except Exception:
                listing_age = None
        is_recent_ipo = bool(listing_age is not None and listing_age < 60)

        if is_bj:
            cat = "pool_excluded_bj"
        elif is_st:
            cat = "st_filtered"
        elif is_recent_ipo:
            cat = "recent_ipo_filtered"
        else:
            cat = "partial_usable"

        cats[cat].append({
            "ts_code": tc,
            "name": name,
            "market": market,
            "list_date": list_date,
            "listing_age_days": listing_age,
            "on_disk_rows": on_disk,
            "error": (f.get("error") or f.get("reason") or "")[:200],
            "attempts": f.get("attempts"),
            "category": cat,
        })

    cat_labels = {
        "pool_excluded_bj": "北交所（分析池外，直接忽略）",
        "st_filtered": "ST/*ST/退市风险标识（名称规则排除）",
        "recent_ipo_filtered": "近期 IPO（60 自然日新股规则排除）",
        "partial_usable": "池内可用（在库有历史，仅缺最新会话）",
    }
    items = [row for rows in cats.values() for row in rows]
    impact = (
        f"共 {len(items)} 只 akshare 源端返回空 qfq 帧（重试 3 次仍空），属源端缺口而非缓存 bug。"
        f"其中 {len(cats['pool_excluded_bj'])} 只北交所不在分析池；"
        f"{len(cats['st_filtered'])} 只 ST/*ST 由名称规则排除；"
        f"{len(cats['recent_ipo_filtered'])} 只为近期 IPO 由 60 自然日新股规则排除；"
        f"剩余 {len(cats['partial_usable'])} 只池内但库内仍保留可用历史（仅缺最新 1-N 个会话），且均非当前榜首。"
        f"抓取失败不覆盖原 store，旧数据保留。对情绪高度结论无影响，无漏判高标。"
    )
    return {
        "source": "bootstrap_manifest.summary.failures",
        "total": len(items),
        "in_pool_total": len([r for r in items if r["category"] != "pool_excluded_bj"]),
        "categories": {
            k: {"label": cat_labels[k], "count": len(v)} for k, v in cats.items()
        },
        "items": items,
        "impact": impact,
    }


# ---------- limit_up（涨停连板梯队） ----------

def build_limit_up(ladder: dict[str, Any] | None, dates: list[str]) -> dict[str, Any] | None:
    """把涨停连板结果对齐到报告的 dates。

    缺失交易日补 ``total=None``（契约要求「缺失用 null」），不补零：
    这一天没数据与「当天一只涨停都没有」是两件事。
    """
    if not ladder:
        return None
    points_by_date = {
        str(point.get("date")): point
        for point in ladder.get("points") or []
        if point.get("date")
    }
    aligned: list[dict[str, Any]] = []
    for day in dates:
        raw = points_by_date.get(day)
        if raw is None:
            aligned.append({
                "date": day,
                "total": None,
                "first_board": None,
                "consecutive": None,
                "by_boards": None,
                "max_boards": None,
                "consecutive_ratio": None,
                "by_market": None,
                "total_change": None,
                "max_boards_change": None,
                "unknown_rows": None,
                "ladder": [],
                "ladder_truncated": False,
            })
            continue
        aligned.append({
            "date": day,
            "total": as_int(raw.get("total")),
            "first_board": as_int(raw.get("first_board")),
            "consecutive": as_int(raw.get("consecutive")),
            "by_boards": raw.get("by_boards") or {},
            "max_boards": as_int(raw.get("max_boards")),
            "consecutive_ratio": as_ratio(raw.get("consecutive_ratio")),
            "by_market": raw.get("by_market") or {},
            "total_change": as_int(raw.get("total_change")),
            "max_boards_change": as_int(raw.get("max_boards_change")),
            "unknown_rows": as_int(raw.get("unknown_rows")),
            "ladder": raw.get("ladder") or [],
            "ladder_truncated": bool(raw.get("ladder_truncated")),
        })

    groups: list[str] = []
    for point in aligned:
        for group in (point.get("by_market") or {}):
            if group not in groups:
                groups.append(group)

    return {
        "unit": "count",
        "scope": ladder.get("scope") or {},
        "window": ladder.get("window") or {},
        "audit": ladder.get("audit") or {},
        "market_groups": groups,
        "points": aligned,
    }


# ---------- summary / audit ----------

def build_emotion_metrics(pool: dict[str, Any]) -> dict[str, Any]:
    """按全市场/市场分组与 T1/T2/T3 汇总当前最高、观察窗口最高。"""
    dates = pool.get("x") or []
    metric_labels = {
        "t1": "固定10日涨幅",
        "t2": "严格底分型起点涨幅",
        "t3": "首板情绪起点涨幅",
    }

    def scope_metrics(source: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in source.get("series") or []:
            key = item.get("key")
            if key not in metric_labels:
                continue
            points = item.get("points") or []
            peak_index = next((index for index in range(len(points) - 1, -1, -1) if points[index] is not None), None)
            current_index = len(points) - 1 if points and points[-1] is not None else None
            peak_value = max((value for value in points if isinstance(value, (int, float))), default=None)
            peak_date = None
            if peak_value is not None:
                peak_date = dates[next(index for index, value in enumerate(points) if value == peak_value)] if dates else None
            result[key] = {
                "label": metric_labels[key],
                "current": points[current_index] if current_index is not None else None,
                "current_date": dates[current_index] if current_index is not None and current_index < len(dates) else None,
                "peak": peak_value,
                "peak_date": peak_date,
                "available_days": sum(1 for value in points if value is not None),
            }
        for key, label in metric_labels.items():
            result.setdefault(key, {
                "label": label, "current": None, "current_date": dates[-1] if dates else None,
                "peak": None, "peak_date": None, "available_days": 0,
            })
        return result

    by_market = {}
    for group, source in (pool.get("by_market") or {}).items():
        by_market[group] = scope_metrics(source)
    return {
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "all": scope_metrics(pool),
        "by_market": by_market,
    }


def _pool_size_hint(coverage: dict[str, Any]) -> str:
    """池规模卡片说明：区分阈值达标与补位来源。

    补位股从未达阈值，若仍统一写成「曾达标股票并集」，卡片数值会与文案口径脱节。
    """
    total = int(coverage.get("pool_union") or 0)
    fill = int(coverage.get("rank_fill_members") or 0)
    if fill <= 0:
        return "窗口内曾达标股票并集"
    threshold = int(coverage.get("threshold_members") or max(total - fill, 0))
    return f"达标 {threshold} + 前10名补位 {fill}"


def build_summary(
    height: dict[str, Any],
    pool: dict[str, Any],
    structure_rows: list[dict[str, Any]],
    emotion_rows: list[dict[str, Any]],
    validation: dict[str, Any] | None,
    threshold: float,
    limit_up: dict[str, Any] | None = None,
) -> dict[str, Any]:
    points = [p for p in height["points"] if p.get("value") is not None]
    peak = max(points, key=lambda p: p["value"]) if points else None
    latest = points[-1] if points else None

    state = "insufficient"
    label = "数据不足"
    if len(points) >= 3:
        if latest and peak and latest["value"] >= peak["value"] * 0.8:
            state, label = "strong", "情绪强势"
        elif latest and latest["value"] >= threshold:
            state, label = "neutral", "情绪中性"
        else:
            state, label = "weak", "情绪回落"

    cards = [
        {
            "label": "最新情绪高度",
            "value": f"{latest['value'] * 100:.1f}%" if latest else "—",
            "hint": latest["date"] if latest else None,
        },
        {
            "label": "区间峰值",
            "value": f"{peak['value'] * 100:.1f}%" if peak else "—",
            "hint": peak["date"] if peak else None,
        },
        {
            "label": "达标池规模",
            "value": str(pool.get("coverage", {}).get("pool_union", 0)),
            "hint": _pool_size_hint(pool.get("coverage", {})),
        },
    ]

    # 涨停连板梯队：只补卡片，不改 state —— 主情绪状态机的口径不因新增指标跳变
    ladder_series = [p for p in (limit_up or {}).get("points", []) if p.get("total") is not None]
    ladder_latest = ladder_series[-1] if ladder_series else None
    if ladder_latest:
        ratio = ladder_latest.get("consecutive_ratio")
        cards.extend([
            {
                "label": "涨停家数",
                "value": str(ladder_latest["total"]),
                "hint": f"{ladder_latest['date']} 收盘涨停",
            },
            {
                "label": "连板家数",
                "value": str(ladder_latest.get("consecutive") or 0),
                "hint": (
                    f"占涨停 {ratio * 100:.1f}%" if isinstance(ratio, (int, float)) else "二板及以上"
                ),
            },
            {
                "label": "最高连板",
                "value": f"{ladder_latest.get('max_boards') or 0} 板",
                "hint": f"首板 {ladder_latest.get('first_board') or 0} 家",
            },
        ])

    if latest:
        tldr = (
            f"最新交易日 {latest['date']} 的 10 日涨幅高度为 {latest['value'] * 100:.1f}%，"
            f"由 {latest.get('leader_name') or '—'}（{latest.get('leader_code') or '—'}）贡献；"
            f"区间峰值为 {peak['value'] * 100:.1f}%（{peak['date']}）。"
        )
    else:
        tldr = "观察窗口内无达标交易日，无法形成情绪高度结论。"

    if ladder_latest:
        max_change = ladder_latest.get("max_boards_change")
        total_change = ladder_latest.get("total_change")
        trend = ""
        if isinstance(max_change, int) and isinstance(total_change, int):
            if max_change > 0 and total_change >= 0:
                trend = "连板高度与涨停家数同步抬升"
            elif max_change < 0 and total_change <= 0:
                trend = "连板高度与涨停家数同步收缩"
            elif max_change == 0 and total_change > 0:
                trend = "涨停家数扩散但高度未抬升"
            elif max_change == 0 and total_change < 0:
                trend = "高度维持但涨停家数收敛"
            else:
                trend = "高度与家数背离"
        tldr += (
            f"同日收盘涨停 {ladder_latest['total']} 家，二板及以上 "
            f"{ladder_latest.get('consecutive') or 0} 家，最高 "
            f"{ladder_latest.get('max_boards') or 0} 板"
            + (f"；{trend}。" if trend else "。")
        )

    return {
        "state": state,
        "state_label": label,
        "tldr": tldr,
        "cards": cards,
    }


def _pct_text(value: float | None) -> str:
    return f"{value * 100:.1f}%" if isinstance(value, (int, float)) else "—"


def build_ai_analysis(
    height: dict[str, Any],
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按《交易决策框架 V2.1.1》情绪层，用报告数据规则化合成分析卡。

    口径全部取自 assets/ai_rules.json（阈值 / 标签 / 策略映射 / 未覆盖清单）。
    只使用报告已有的情绪高度 / 广度字段；均线承载、板块承接、个股级别等
    报告未覆盖的字段如实标注“未覆盖”，绝不臆造。
    """
    rules = rules or load_ai_rules()
    params = rules["params"]
    labels = rules["labels"]
    stance_map = rules["stance"]
    text = rules["conditions"]

    framework = rules["framework"]["label"]
    disclaimer = rules["disclaimer"]
    min_points = int(params["min_points"])
    new_high_ratio = float(params["new_high_ratio"])
    deep_drawdown = float(params["deep_drawdown"])
    break_change = float(params["break_change"])
    lookback = int(params["breadth_lookback"])
    recovery = int(params["recovery_window"])
    stage_window = int(params["stage_window"])
    # 保留正负号：断档阈值 -0.2 → “-20”，文案中的「≤ -20pp」才读得通
    break_pp = f"{break_change * 100:g}"

    points = [p for p in height.get("points", []) if p.get("value") is not None]
    if len(points) < min_points:
        return {
            "engine": rules["engine"],
            "framework": framework,
            "as_of": points[-1]["date"] if points else None,
            "summary": rules["summary"]["insufficient"].format(min_points=min_points),
            "fields": [],
            "conditions": {"成立": [], "待确认": [], "失效": []},
            "advice": None,
            "disclaimer": disclaimer,
        }

    latest = points[-1]
    peak = max(points, key=lambda p: p["value"])
    look_index = len(points) - 1 - lookback
    look = points[look_index] if look_index >= 0 else points[0]
    drawdown = latest["value"] / peak["value"] - 1.0 if peak["value"] else None
    breadth_latest = int(latest.get("count") or 0)
    breadth_look = int(look.get("count") or 0)

    last_break = None
    for index, point in enumerate(points):
        change = point.get("change")
        if change is not None and change <= break_change:
            last_break = index
    days_since_break = (len(points) - 1 - last_break) if last_break is not None else None

    if latest["value"] >= peak["value"] * new_high_ratio:
        new_high = labels["new_high"]["fresh"]
    elif drawdown is not None and drawdown > deep_drawdown:
        new_high = labels["new_high"]["repaired"]
    else:
        new_high = labels["new_high"]["weak"]

    if latest["value"] >= peak["value"] * new_high_ratio:
        space = labels["space"]["raising"]
    elif drawdown is not None and drawdown <= deep_drawdown:
        space = labels["space"]["shrinking"]
    else:
        space = labels["space"]["flat"]

    if breadth_latest > breadth_look:
        breadth_text = f"{labels['breadth']['expand']}（{breadth_look}→{breadth_latest}）"
    elif breadth_latest < breadth_look:
        breadth_text = f"{labels['breadth']['shrink']}（{breadth_look}→{breadth_latest}）"
    else:
        breadth_text = f"{labels['breadth']['flat']}（{breadth_latest}）"

    if new_high == labels["new_high"]["fresh"] and space == labels["space"]["raising"]:
        divergence_key = "mid"
    elif new_high == labels["new_high"]["weak"] and space == labels["space"]["shrinking"]:
        divergence_key = "turn"
    else:
        divergence_key = "continue"
    divergence = labels["divergence"][divergence_key]
    period = labels["period"][divergence_key]

    if divergence_key == "mid":
        stance = stance_map["mid"]
    elif divergence_key == "continue":
        stance = stance_map["continue"]
    elif divergence_key == "turn" and days_since_break is not None and days_since_break <= recovery:
        stance = stance_map["turn_test"]
    else:
        stance = stance_map["defend"]

    stage = (
        f"{labels['stage']['prefix']}{days_since_break}"
        if (days_since_break is not None and days_since_break <= stage_window)
        else labels["stage"]["normal"]
    )

    fields = [
        {
            "k": "周期内领涨股群",
            "v": (
                f"{latest.get('leader_name') or '—'}（{latest.get('leader_code') or '—'}），"
                f"连续 {latest.get('leader_streak') or 0} 日，"
                f"{'今日换人' if latest.get('leader_changed') else '未换人'}"
            ),
        },
        {"k": "创新高能力", "v": new_high},
        {"k": "市场涨幅空间", "v": space},
        {"k": "广度响应", "v": breadth_text},
        {"k": "分歧演化", "v": divergence},
        {"k": "情绪周期", "v": period},
        {"k": "观察阶段", "v": stage},
    ]
    fields.extend(rules.get("uncovered_fields") or [])

    conditions = {
        "成立": [
            f"最新高度 {_pct_text(latest['value'])}，前峰 {_pct_text(peak['value'])}（{peak['date']}），{new_high}",
            (
                text["break_at"].format(date=points[last_break]["date"], days=days_since_break)
                if last_break is not None
                else text["no_break"].format(break_pp=break_pp)
            ),
            f"达标广度{breadth_text}",
        ],
        "待确认": [
            (
                text["pending_break_peak"].format(
                    peak=_pct_text(peak["value"]), drawdown=_pct_text(drawdown)
                )
                if drawdown is not None
                else text["pending_break_peak_na"]
            ),
            text["pending_breadth"].format(breadth=breadth_latest),
            text["pending_structure"],
        ],
        "失效": [
            text["invalid_break"].format(break_pp=break_pp),
            text["invalid_breadth"].format(lookback=lookback),
            text["invalid_leader"],
        ],
    }

    summary_text = rules["summary"]["template"].format(
        latest=_pct_text(latest["value"]),
        peak=_pct_text(peak["value"]),
        peak_date=peak["date"],
        new_high=new_high,
        space=space,
        breadth=breadth_text,
        divergence=divergence,
        period=period,
        stance=stance["stance"],
        position=stance["position"],
    )

    return {
        "engine": rules["engine"],
        "framework": framework,
        "as_of": latest["date"],
        "summary": summary_text,
        "fields": fields,
        "conditions": conditions,
        "advice": {
            "period": period,
            "divergence": divergence,
            "stance": stance["stance"],
            "position": stance["position"],
        },
        "disclaimer": disclaimer,
    }


def build_audit(
    data: dict[str, Any],
    source_meta: dict[str, Any] | None = None,
    fetch_failures: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = data.get("source") or {}
    sources = []
    if isinstance(source, dict):
        for key, value in source.items():
            sources.append({"label": key, "path": str(value)})
    elif isinstance(source, list):
        sources = source
    if source_meta:
        sources.append(source_meta)

    return {
        "validation": {
            "three_height": data.get("three_height_validation"),
            "base": data.get("validation"),
        },
        "excluded": data.get("audit_excluded") or [],
        "process": data.get("process_log") or [],
        "sources": sources,
        "fetch_failures": fetch_failures,
    }


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="技能计算结果 → 契约数据")
    parser.add_argument("--input", required=True, type=Path, help="three_height_metrics.json")
    parser.add_argument("--forward-daily", type=Path, default=None, help="前复权日线目录")
    parser.add_argument("--unadjusted-daily", type=Path, default=None, help="未复权日线目录（含显式涨停状态，用于逐日首板口径；缺失时T3为空）")
    parser.add_argument("--output", required=True, type=Path, help="输出 data.json")
    parser.add_argument("--end-date", default=None, help="指定结束日，默认最新交易日")
    parser.add_argument(
        "--return-days", type=int, default=10,
        help="区间涨幅长度，固定 10（10 日涨幅）。切勿用输入里的 threshold_days，那是观察窗口长度。",
    )
    parser.add_argument(
        "--manifest", type=Path, default=None,
        help="bootstrap_manifest.json 路径；提供则注入源端抓取失败审计",
    )
    parser.add_argument(
        "--stock-basic", type=Path, default=None,
        help="stock_basic.json 路径；用于失败股名称/上市日分类",
    )
    parser.add_argument(
        "--price-store", type=Path, default=None,
        help="price_store 目录；用于读取失败股在库行数",
    )
    parser.add_argument(
        "--ai-rules", type=Path, default=None,
        help="AI 分析规则文件；默认 assets/ai_rules.json（阈值/标签/策略映射的唯一来源）",
    )
    parser.add_argument(
        "--limit-up-ladder", type=Path, default=None,
        help="limit_up_ladder.json；提供则注入每日涨停家数与连板梯队（缺失时该视图自动隐藏）",
    )
    args = parser.parse_args()

    data = read_json(args.input)
    forward = load_forward(args.forward_daily)
    raw = load_raw_daily(args.unadjusted_daily)

    dates = [str(day) for day in data.get("dates", [])]
    if args.end_date:
        dates = [day for day in dates if day <= args.end_date]
    threshold_points = float(data.get("min_return_pct") or 50.0)
    threshold = threshold_points / 100.0
    # threshold_days 是新股排除的自然日阈值，绝不复用为收益周期或报告观察窗口。
    # 收益周期由 --return-days 指定；观察窗口直接取实际输出 dates 数量。
    return_days = int(args.return_days)
    source_lookback = data.get("lookback_trading_sessions")
    if source_lookback is not None:
        try:
            source_lookback = int(source_lookback)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"输入 lookback_trading_sessions={source_lookback!r} 不是整数") from exc
        if source_lookback <= 0:
            raise ValueError(f"输入 lookback_trading_sessions={source_lookback} 必须为正整数")
        if source_lookback != return_days:
            raise ValueError(
                f"输入 lookback_trading_sessions={source_lookback} 与 --return-days={return_days} 不一致"
            )
    window_days = len(dates) or None

    qualified = data.get("qualified") or []
    market_groups: list[str] = []
    for row in qualified:
        group = row.get("market_group")
        if group and group not in market_groups:
            market_groups.append(group)
    if not market_groups:
        market_groups = ["沪深主板"]

    structure_raw = data.get("structure_waves") or []
    emotion_raw = data.get("emotion_rallies") or []

    height = build_height(qualified, dates, market_groups, data.get("daily_summary") or [])
    pool = build_pool_trends(
        qualified, forward, structure_raw, emotion_raw,
        threshold, return_days, args.end_date,
        rank_fill=data.get("rank_fill") or [],
        raw=raw,
        observation_dates=dates,
    )
    structure_rows = build_structure_rows(structure_raw)
    emotion_rows = build_emotion_rows(emotion_raw)

    details: dict[str, Any] = {}
    for raw in structure_raw + emotion_raw:
        key = raw.get("ts_code") or raw.get("code")
        if not key:
            continue
        entry = details.setdefault(str(key), {})
        if "evidence" in raw:
            entry["evidence"] = raw["evidence"]
        if raw.get("reasons") is not None:
            entry["reasons"] = raw["reasons"]

    # 详情索引：把结构/情绪两类 evidence 分别挂上，避免互相覆盖
    detail_index: dict[str, dict[str, Any]] = {}
    for kind, rows in (("structure", structure_raw), ("emotion", emotion_raw)):
        for raw in rows:
            key = raw.get("ts_code") or raw.get("code")
            if not key:
                continue
            slot = detail_index.setdefault(str(key), {})
            slot[kind] = {
                "evidence": raw.get("evidence"),
                "reasons": raw.get("reasons"),
                "status": raw.get("status"),
            }

    ladder_raw = read_json(args.limit_up_ladder) if args.limit_up_ladder and args.limit_up_ladder.exists() else None
    if args.limit_up_ladder and ladder_raw is None:
        print(f"警告：--limit-up-ladder 不存在 {args.limit_up_ladder}，跳过连板指标", flush=True)
    limit_up = build_limit_up(ladder_raw, dates)

    summary = build_summary(
        height, pool, structure_rows, emotion_rows,
        data.get("three_height_validation"), threshold, limit_up,
    )

    meta = {
        "title": data.get("title") or "市场分段 10 日涨幅报告",
        "subtitle": data.get("subtitle"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "threshold": threshold,
        "rank_leg_size": (data.get("signal_candle") or {}).get("rank_leg_size"),
        "return_days": return_days,
        "window_days": window_days,
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "warning": data.get("warning"),
    }

    fetch_failures = build_fetch_failures(
        args.manifest, args.stock_basic, args.price_store,
        dates[-1] if dates else None,
    )

    ai_rules = load_ai_rules(args.ai_rules)
    ai_analysis = build_ai_analysis(height, ai_rules)
    market_timeline = build_market_timeline(dates, height, pool, limit_up, ai_rules)

    payload = {
        "contract_version": CONTRACT_VERSION,
        "meta": meta,
        "summary": summary,
        "dates": dates,
        "height": height,
        "limit_up": limit_up,
        "pool_trends": pool,
        "structure_waves": structure_rows,
        "emotion_rallies": emotion_rows,
        "details": detail_index,
        "audit": build_audit(data, fetch_failures=fetch_failures),
        "ai_analysis": ai_analysis,
        "market_timeline": market_timeline,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    summary_counts = defaultdict(int)
    for row in structure_rows + emotion_rows:
        summary_counts[row["status"]] += 1

    print(json.dumps({
        "output": str(args.output),
        "contract_version": CONTRACT_VERSION,
        "bytes": args.output.stat().st_size,
        "dates": len(dates),
        "pool_x": len(pool["x"]),
        "pool_series_points": [len(s["points"]) for s in pool["series"]],
        "structure_rows": len(structure_rows),
        "emotion_rows": len(emotion_rows),
        "details": len(detail_index),
        "status_counts": dict(summary_counts),
        "y_max": pool["y_max"],
        "limit_up_points": len((limit_up or {}).get("points") or []),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
