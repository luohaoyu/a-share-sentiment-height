#!/usr/bin/env python3
"""Calculate first-board emotion rallies from threshold-qualified candidates.

The script is provider-neutral and offline-only. It consumes threshold candidates,
forward-adjusted OHLC, and unadjusted daily rows carrying an explicit limit-up
status. It never infers limit-up status from prices or percentage changes.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

PARAMETERS_VERSION = "emotion-rally-state-machine-v2-cache-bounded-search"
SUPPORTED_SUFFIXES = {".json", ".csv"}
CODE_FIELDS = (
    "ts_code", "code", "symbol", "stock_code", "security_code", "sec_code",
    "股票代码", "证券代码", "代码",
)
DATE_FIELDS = (
    "trade_date", "date", "cal_date", "datetime", "time", "交易日期", "日期",
)
NAME_FIELDS = ("name", "stock_name", "sec_name", "security_name", "股票名称", "证券简称", "名称")
MARKET_FIELDS = ("market", "market_label", "market_source", "board", "市场", "板块")
MARKET_GROUP_FIELDS = ("market_group", "market_group_label", "市场分组", "组合市场")
HIGH_FIELDS = ("high", "adj_high", "qfq_high", "forward_high", "最高价", "前复权最高价")
CLOSE_FIELDS = ("close", "adj_close", "qfq_close", "forward_close", "收盘价", "前复权收盘价")
RAW_CLOSE_FIELDS = ("close", "raw_close", "unadjusted_close", "close_raw", "收盘价", "未复权收盘价")
LIMIT_FIELDS = (
    "is_limit_up", "limit_up", "is_up_limit", "up_limit", "limit_status",
    "zt", "is_zt", "涨停状态", "是否涨停", "涨停",
)
HIT_DATE_FIELDS = ("hit_date", "threshold_date", "trade_date", "date", "cal_date", "命中日期", "日期")
BASE_DATE_FIELDS = ("base_date", "window_base_date", "start_base_date", "基准日", "窗口基准日")


def first_value(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_date(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if not text:
        raise ValueError("empty date")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    elif len(text) >= 10:
        text = text[:10]
    return date.fromisoformat(text).isoformat()


def normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    suffix = ""
    if "." in text:
        symbol_part, suffix = text.split(".", 1)
    else:
        symbol_part = text
    digits = "".join(ch for ch in symbol_part if ch.isdigit())
    if not digits:
        digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return text
    symbol = digits[-6:].zfill(6)
    suffix = {"XSHG": "SH", "SSE": "SH", "XSHE": "SZ", "SZSE": "SZ", "BSE": "BJ"}.get(suffix, suffix)
    if not suffix:
        if symbol.startswith(("600", "601", "603", "605", "688")):
            suffix = "SH"
        elif symbol.startswith(("000", "001", "002", "003", "300", "301")):
            suffix = "SZ"
        elif symbol.startswith(("43", "83", "87", "92")):
            suffix = "BJ"
    return f"{symbol}.{suffix}" if suffix else symbol


def bare_code(code: str) -> str:
    """返回 6 位裸代码，使情绪记录的 `code` 与结构波段/基础候选行的 `code` 语义一致。"""
    return code.split(".", 1)[0] if "." in code else code


def finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite {field}: {value!r}")
    return number


def parse_limit_status(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        if float(value) == 1.0:
            return True
        if float(value) == 0.0:
            return False
        raise ValueError(f"explicit limit-up status must be 0 or 1, got {value!r}")
    text = str(value).strip().lower().replace(" ", "")
    true_values = {"1", "true", "yes", "y", "是", "涨停", "封板", "limit_up", "up_limit"}
    false_values = {"0", "false", "no", "n", "否", "未涨停", "非涨停", "未封板", "normal", "not_limit_up"}
    if text in true_values:
        return True
    if text in false_values:
        return False
    raise ValueError(f"unrecognized explicit limit-up status: {value!r}")


def reason_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        text = str(item.get("reason") or item.get("reasons") or "").strip()
        if not text:
            parts = [f"{key}={value}" for key, value in item.items() if key not in ("reason", "reasons")]
            text = "; ".join(parts)
        return text
    return str(item)


def infer_code_from_path(path: Path) -> str:
    matches = re.findall(r"(?<!\d)(\d{6})(?!\d)", path.stem)
    return normalize_code(matches[-1]) if matches else ""


def json_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("fields"), list) and isinstance(data.get("items"), list):
        fields = data["fields"]
        return [dict(zip(fields, item)) for item in data["items"] if isinstance(item, list)]
    for key in ("rows", "records", "items", "result", "data"):
        nested = payload.get(key)
        if isinstance(nested, list):
            return [dict(item) for item in nested if isinstance(item, dict)]
    if first_value(payload, CODE_FIELDS) is not None or first_value(payload, DATE_FIELDS) is not None:
        return [dict(payload)]
    rows: list[dict[str, Any]] = []
    for key, nested in payload.items():
        if isinstance(nested, list) and all(isinstance(item, dict) for item in nested):
            inherited_code = normalize_code(key)
            for item in nested:
                copy = dict(item)
                if inherited_code and first_value(copy, CODE_FIELDS) is None:
                    copy["code"] = inherited_code
                rows.append(copy)
    return rows


def input_files(path: Path, codes: set[str] | None = None) -> list[Path]:
    """List input files, optionally skipping files whose filename encodes a non-candidate code.

    Full-market caches hold thousands of per-symbol files; only candidate symbols can
    affect the result, so the candidate set is used as a filename pre-filter. Files
    whose name carries no 6-digit code are always kept, so data layouts that cannot be
    filtered by name degrade to the previous full-scan behaviour instead of losing rows.
    """
    if path.is_file():
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError(f"unsupported input file: {path}")
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)
    files = sorted(item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES)
    if not files:
        raise ValueError(f"no JSON/CSV files found under {path}")
    if codes:
        def keep(file: Path) -> bool:
            code = infer_code_from_path(file)
            return not code or code in codes

        files = [file for file in files if keep(file)]
        if not files:
            raise ValueError(f"no candidate ({len(codes)} codes) files found under {path}")
    return files


def read_rows(
    path: Path,
    qualified_only: bool = False,
    codes: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {"files": []}
    for file in input_files(path, codes):
        metadata["files"].append(str(file))
        code_hint = infer_code_from_path(file)
        if file.suffix.lower() == ".csv":
            with file.open("r", encoding="utf-8-sig", newline="") as handle:
                file_rows = [dict(row) for row in csv.DictReader(handle)]
        else:
            payload = json.loads(file.read_text(encoding="utf-8-sig"))
            if qualified_only and isinstance(payload, dict) and isinstance(payload.get("qualified"), list):
                file_rows = [dict(item) for item in payload["qualified"] if isinstance(item, dict)]
                for key in ("lookback_trading_sessions", "min_return_pct", "generated_at"):
                    if key in payload:
                        metadata[key] = payload[key]
            else:
                file_rows = json_records(payload)
                if isinstance(payload, dict):
                    for key in ("lookback_trading_sessions", "min_return_pct", "generated_at"):
                        if key in payload:
                            metadata[key] = payload[key]
        for row in file_rows:
            copy = dict(row)
            if first_value(copy, CODE_FIELDS) is None and code_hint:
                copy["code"] = code_hint
            copy["__source_file"] = str(file)
            rows.append(copy)
    return rows, metadata


def parse_date_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[,;|\s]+", str(value).strip())
    result = []
    for item in values:
        if item not in (None, ""):
            result.append(normalize_date(item))
    return result


def load_candidates(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, metadata = read_rows(path, qualified_only=True)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = normalize_code(first_value(row, CODE_FIELDS))
        if not code:
            raise ValueError(f"qualified candidate lacks stock code in {row.get('__source_file')}")
        item = grouped.setdefault(code, {
            "code": code,
            "name": str(first_value(row, NAME_FIELDS) or code),
            "market": first_value(row, MARKET_FIELDS),
            "market_group": first_value(row, MARKET_GROUP_FIELDS),
            "hit_dates": set(),
            "windows": [],
            "source_rows": 0,
            "source_files": set(),
        })
        incoming_market = first_value(row, MARKET_FIELDS)
        incoming_group = first_value(row, MARKET_GROUP_FIELDS)
        if incoming_market not in (None, ""):
            if item["market"] not in (None, "") and str(item["market"]) != str(incoming_market):
                raise ValueError(f"conflicting market for qualified candidate {code}")
            item["market"] = str(incoming_market)
        if incoming_group not in (None, ""):
            if item["market_group"] not in (None, "") and str(item["market_group"]) != str(incoming_group):
                raise ValueError(f"conflicting market_group for qualified candidate {code}")
            item["market_group"] = str(incoming_group)
        item["source_rows"] += 1
        item["source_files"].add(row.get("__source_file"))
        explicit_hits = parse_date_list(row.get("hit_dates"))
        raw_hit = first_value(row, HIT_DATE_FIELDS)
        if raw_hit not in (None, ""):
            explicit_hits.append(normalize_date(raw_hit))
        if not explicit_hits:
            raise ValueError(f"qualified candidate {code} lacks hit date")
        raw_base = first_value(row, BASE_DATE_FIELDS)
        base_date = normalize_date(raw_base) if raw_base not in (None, "") else None
        for hit_date in sorted(set(explicit_hits)):
            item["hit_dates"].add(hit_date)
            item["windows"].append({"hit_date": hit_date, "base_date": base_date})
    candidates = []
    for code in sorted(grouped):
        item = grouped[code]
        item["hit_dates"] = sorted(item["hit_dates"])
        item["windows"] = sorted(
            {((row["hit_date"]), row["base_date"]) for row in item["windows"]},
            key=lambda pair: (pair[0], pair[1] or ""),
        )
        item["windows"] = [{"hit_date": hit, "base_date": base} for hit, base in item["windows"]]
        item["source_files"] = sorted(path for path in item["source_files"] if path)
        candidates.append(item)
    return candidates, metadata


def merge_value(existing: Any, incoming: Any, label: str, key: tuple[str, str]) -> Any:
    if incoming is None:
        return existing
    if existing is None:
        return incoming
    if isinstance(existing, float) and isinstance(incoming, float):
        if math.isclose(existing, incoming, rel_tol=1e-12, abs_tol=1e-12):
            return existing
    elif existing == incoming:
        return existing
    raise ValueError(f"conflicting duplicate {label} for {key}: {existing!r} vs {incoming!r}")


def load_daily(
    path: Path,
    adjusted: bool,
    codes: set[str] | None = None,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    rows, metadata = read_rows(path, codes=codes)
    if codes:
        metadata["code_filter"] = {
            "applied": True,
            "candidate_codes": len(codes),
            "files_read": len(metadata["files"]),
        }
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        code = normalize_code(first_value(row, CODE_FIELDS))
        raw_date = first_value(row, DATE_FIELDS)
        if not code or raw_date in (None, ""):
            continue
        trade_date = normalize_date(raw_date)
        key = (code, trade_date)
        target = result.setdefault(code, {}).setdefault(trade_date, {
            "date": trade_date,
            "source_files": set(),
        })
        target["source_files"].add(row.get("__source_file"))
        if adjusted:
            raw_high = first_value(row, HIGH_FIELDS)
            raw_close = first_value(row, CLOSE_FIELDS)
            high = finite_number(raw_high, "forward-adjusted high") if raw_high not in (None, "") else None
            close = finite_number(raw_close, "forward-adjusted close") if raw_close not in (None, "") else None
            target["high"] = merge_value(target.get("high"), high, "forward-adjusted high", key)
            target["close"] = merge_value(target.get("close"), close, "forward-adjusted close", key)
        else:
            raw_close = first_value(row, RAW_CLOSE_FIELDS)
            close = finite_number(raw_close, "unadjusted close") if raw_close not in (None, "") else None
            raw_limit = first_value(row, LIMIT_FIELDS)
            status = parse_limit_status(raw_limit)
            target["close"] = merge_value(target.get("close"), close, "unadjusted close", key)
            target["is_limit_up"] = merge_value(target.get("is_limit_up"), status, "limit-up status", key)
    for code_rows in result.values():
        for row in code_rows.values():
            row["source_files"] = sorted(path for path in row["source_files"] if path)
    return result, metadata


def window_indices(
    candidate: dict[str, Any],
    dates: list[str],
) -> tuple[set[int], dict[int, set[str]], list[dict[str, Any]]]:
    index_by_date = {trade_date: index for index, trade_date in enumerate(dates)}
    selected: set[int] = set()
    hits_by_index: dict[int, set[str]] = {}
    issues: list[dict[str, Any]] = []
    for window in candidate["windows"]:
        hit_date = window["hit_date"]
        hit_index = index_by_date.get(hit_date)
        if hit_index is None:
            issues.append({"reason": "hit_date_missing_from_forward_daily", "hit_date": hit_date})
            continue
        # 首板搜索不再受候选窗口 base_date 或收益计算 lookback 的限制。
        # base_date 仅属于 10 日收益筛选证据；首板起点应在当前缓存可见范围内
        # 从最早会话搜索到 hit_date，唯一硬边界是缓存的 dates 起点。
        start_index = 0
        if start_index > hit_index:
            issues.append({"reason": "invalid_candidate_window", "hit_date": hit_date})
            continue
        for index in range(start_index, hit_index + 1):
            selected.add(index)
            hits_by_index.setdefault(index, set()).add(hit_date)
    return selected, hits_by_index, issues


def identify_first_boards(
    candidate: dict[str, Any],
    dates: list[str],
    raw_rows: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    selected, hits_by_index, issues = window_indices(candidate, dates)
    confirmed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = list(issues)
    limit_up_days: list[str] = []
    missing_days: list[str] = []
    for index in sorted(selected):
        trade_date = dates[index]
        status = raw_rows.get(trade_date, {}).get("is_limit_up")
        if status is None:
            missing_days.append(trade_date)
            insufficient.append({
                "reason": "limit_status_missing_in_first_board_search",
                "date": trade_date,
                "related_hit_dates": sorted(hits_by_index.get(index, set())),
            })
            continue
        if status is not True:
            continue
        limit_up_days.append(trade_date)
        required_indices = list(range(index - 5, index))
        if required_indices[0] < 0:
            insufficient.append({
                "reason": "insufficient_prior_sessions_for_first_board",
                "date": trade_date,
                "required_prior_sessions": 5,
                "available_prior_sessions": index,
            })
            continue
        status_evidence = []
        missing_dates = []
        prior_limit_dates = []
        for prior_index in required_indices:
            prior_date = dates[prior_index]
            prior_status = raw_rows.get(prior_date, {}).get("is_limit_up")
            status_evidence.append({"date": prior_date, "is_limit_up": prior_status})
            if prior_status is None:
                missing_dates.append(prior_date)
            elif prior_status is True:
                prior_limit_dates.append(prior_date)
        # 判定顺序有讲究：前 5 个交易日内只要有一个已确认涨停，就已经足以否定「首板」，
        # 此时剩下的缺失状态再怎么补也不改变结论。若先判缺失，会把「续板」误报成
        # data_insufficient，掩盖「首板在搜索窗口之前」这个真实成因。
        if prior_limit_dates:
            rejected.append({
                "date": trade_date,
                "reason": "prior_five_sessions_contain_limit_up",
                "prior_limit_up_dates": prior_limit_dates,
                "status_evidence": status_evidence,
            })
            continue
        if missing_dates:
            insufficient.append({
                "reason": "prior_limit_status_missing_for_first_board",
                "date": trade_date,
                "missing_dates": missing_dates,
                "status_evidence": status_evidence,
            })
            continue
        confirmed.append({
            "index": index,
            "date": trade_date,
            "related_hit_dates": sorted(hits_by_index.get(index, set())),
            "status_evidence": status_evidence + [{"date": trade_date, "is_limit_up": True}],
        })
    ordered = sorted(selected)
    diagnostics = {
        "searched_session_count": len(ordered),
        "searched_from": dates[ordered[0]] if ordered else None,
        "searched_to": dates[ordered[-1]] if ordered else None,
        "search_reached_cache_start": bool(ordered and ordered[0] == 0),
        "limit_up_days_in_window": limit_up_days,
        "earliest_limit_up_in_window": limit_up_days[0] if limit_up_days else None,
        "missing_limit_status_days_in_window": missing_days,
    }
    return confirmed, rejected, insufficient, diagnostics


def style_for_interval(
    dates: list[str],
    raw_rows: dict[str, dict[str, Any]],
    start_index: int,
    end_index: int,
) -> tuple[str, dict[str, Any]]:
    statuses = [raw_rows.get(dates[index], {}).get("is_limit_up") for index in range(start_index, end_index + 1)]
    missing_dates = [dates[start_index + offset] for offset, status in enumerate(statuses) if status is None]
    true_count = sum(status is True for status in statuses)
    total = len(statuses)
    ratio = true_count / total if total else None
    if missing_dates or ratio is None:
        style = "unknown"
    elif ratio >= 0.5:
        style = "连板型"
    elif ratio < 0.3:
        style = "趋势型"
    else:
        style = "混合型"
    return style, {
        "interval_sessions": total,
        "explicit_limit_up_sessions": true_count,
        "limit_up_ratio": round(ratio, 8) if ratio is not None and not missing_dates else None,
        "missing_limit_status_dates": missing_dates,
    }


def track_rally(
    candidate: dict[str, Any],
    first_board: dict[str, Any],
    dates: list[str],
    forward_rows: dict[str, dict[str, Any]],
    raw_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    start_index = int(first_board["index"])
    first_date = dates[start_index]
    status = "pending"
    trend_end_date: str | None = None
    end_index = len(dates) - 1
    failure: dict[str, Any] | None = None
    no_new_high_count = 0
    extensions: list[dict[str, Any]] = []
    high_updates: list[dict[str, Any]] = []

    first_high = forward_rows.get(first_date, {}).get("high")
    if first_high is None or first_high <= 0:
        rolling_high = None
        high_date = None
        status = "data_insufficient"
        end_index = start_index
        failure = {"reason": "forward_adjusted_high_missing_or_invalid", "date": first_date}
    else:
        rolling_high = float(first_high)
        high_date = first_date
        high_updates.append({"date": first_date, "high": rolling_high, "event": "initial_high"})
        for index in range(start_index + 1, len(dates)):
            trade_date = dates[index]
            current_high = forward_rows.get(trade_date, {}).get("high")
            if current_high is None or current_high <= 0:
                status = "data_insufficient"
                end_index = index
                failure = {"reason": "forward_adjusted_high_missing_or_invalid", "date": trade_date}
                break
            if float(current_high) > rolling_high:
                rolling_high = float(current_high)
                high_date = trade_date
                no_new_high_count = 0
                high_updates.append({"date": trade_date, "high": rolling_high, "event": "new_high"})
                continue
            no_new_high_count += 1
            if no_new_high_count < 4:
                continue
            fourth_day_status = raw_rows.get(trade_date, {}).get("is_limit_up")
            if fourth_day_status is None:
                status = "data_insufficient"
                end_index = index
                failure = {"reason": "fourth_no_new_high_day_limit_status_missing", "date": trade_date}
                break
            if fourth_day_status is True:
                extensions.append({
                    "date": trade_date,
                    "reason": "fourth_no_new_high_day_is_limit_up",
                    "action": "reset_no_new_high_window",
                })
                no_new_high_count = 0
                continue
            status = "confirmed"
            trend_end_date = trade_date
            end_index = index
            break

    previous_index = start_index - 1
    adjusted_baseline = None
    raw_baseline = None
    baseline_date = dates[previous_index] if previous_index >= 0 else None
    baseline_issue = None
    if baseline_date is None:
        baseline_issue = {"reason": "first_board_previous_session_unavailable", "date": first_date}
    else:
        adjusted_baseline = forward_rows.get(baseline_date, {}).get("close")
        raw_baseline = raw_rows.get(baseline_date, {}).get("close")
        if adjusted_baseline is None or adjusted_baseline <= 0:
            baseline_issue = {"reason": "forward_adjusted_previous_close_missing_or_invalid", "date": baseline_date}
        elif raw_baseline is None or raw_baseline <= 0:
            baseline_issue = {"reason": "unadjusted_previous_close_missing_or_invalid", "date": baseline_date}
    if baseline_issue and status != "data_insufficient":
        status = "data_insufficient"
        trend_end_date = None
        failure = baseline_issue

    calculation_baseline = None
    baseline_source = None
    scale_consistent = None
    if adjusted_baseline is not None and adjusted_baseline > 0:
        if raw_baseline is not None and raw_baseline > 0:
            scale_consistent = math.isclose(
                float(adjusted_baseline), float(raw_baseline), rel_tol=1e-8, abs_tol=1e-8
            )
        if scale_consistent:
            calculation_baseline = float(raw_baseline)
            baseline_source = "unadjusted_previous_close_same_scale_as_forward_adjusted"
        else:
            calculation_baseline = float(adjusted_baseline)
            baseline_source = "forward_adjusted_previous_close_same_scale_as_forward_high"

    rally_gain_pct = None
    if rolling_high is not None and calculation_baseline is not None and calculation_baseline > 0:
        rally_gain_pct = round((rolling_high / calculation_baseline - 1.0) * 100.0, 8)

    style, style_evidence = style_for_interval(dates, raw_rows, start_index, max(start_index, end_index))
    rally_days = max(0, end_index - start_index)
    evidence = {
        "first_board": {
            "status": "confirmed",
            "rule": "current=true and indices i-5..i-1 all false; i-5..i-2 are the four non-adjacent prior checks",
            "limit_statuses": first_board["status_evidence"],
            "related_hit_dates": first_board["related_hit_dates"],
        },
        "trend_tracking": {
            "rule": "forward-adjusted high; reset on new high; confirm after four consecutive non-new-high sessions unless fourth is explicit limit-up",
            "high_updates": high_updates,
            "fourth_day_limit_up_extensions": extensions,
            "ending_no_new_high_count": no_new_high_count,
            "failure": failure,
        },
        "baseline": {
            "baseline_date": baseline_date,
            "audit_baseline_raw": raw_baseline,
            "forward_adjusted_baseline_close": adjusted_baseline,
            "absolute_scale_consistent": scale_consistent,
            "calculation_baseline": calculation_baseline,
            "calculation_baseline_source": baseline_source,
            "cross_adjustment_division_used": False,
        },
        "style": style_evidence,
    }
    reasons = []
    if failure:
        reasons.append(reason_text(failure))
    elif status == "pending":
        reasons.append(reason_text({
            "reason": "fewer_than_four_consecutive_no_new_high_sessions_before_data_end",
            "evaluation_end_date": dates[end_index] if dates else None,
            "ending_no_new_high_count": no_new_high_count,
        }))
    return {
        "code": bare_code(candidate["code"]),
        "ts_code": candidate["code"],
        "name": candidate["name"],
        "market": candidate.get("market"),
        "market_group": candidate.get("market_group"),
        "hit_dates": candidate["hit_dates"],
        "status": status,
        "first_board_date": first_date,
        "high_date": high_date,
        "trend_end_date": trend_end_date,
        "trend_end_evidence": status,
        "rally_gain_pct": rally_gain_pct,
        "rally_days": rally_days,
        "style": style,
        "reasons": reasons,
        "audit_baseline_raw": raw_baseline,
        "evaluation_end_date": dates[end_index] if dates else None,
        "evidence": evidence,
        "parameters_version": PARAMETERS_VERSION,
    }


def empty_emotion_rally(
    candidate: dict[str, Any],
    status: str,
    reasons: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "code": bare_code(candidate["code"]),
        "ts_code": candidate["code"],
        "name": candidate["name"],
        "market": candidate.get("market"),
        "market_group": candidate.get("market_group"),
        "hit_dates": candidate["hit_dates"],
        "status": status,
        "first_board_date": None,
        "high_date": None,
        "trend_end_date": None,
        "trend_end_evidence": status,
        "rally_gain_pct": None,
        "rally_days": None,
        "style": "unknown",
        "reasons": reasons,
        "evidence": evidence,
        "parameters_version": PARAMETERS_VERSION,
    }


def select_primary_rally(
    candidate: dict[str, Any],
    rallies: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    hit_dates = candidate["hit_dates"]
    covering = [
        rally for rally in rallies
        if any(
            rally["first_board_date"] <= hit_date <= rally["evaluation_end_date"]
            for hit_date in hit_dates
        )
    ]
    if covering:
        return min(covering, key=lambda rally: rally["first_board_date"]), "earliest_rally_covering_any_hit_date"
    latest_hit_date = max(hit_dates)
    preceding = [rally for rally in rallies if rally["first_board_date"] <= latest_hit_date]
    if preceding:
        return max(preceding, key=lambda rally: rally["first_board_date"]), "nearest_first_board_not_later_than_latest_hit_date"
    return None, None


def process_stock(
    candidate: dict[str, Any],
    forward_by_code: dict[str, dict[str, dict[str, Any]]],
    raw_by_code: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    code = candidate["code"]
    forward_rows = forward_by_code.get(code, {})
    raw_rows = raw_by_code.get(code, {})
    base_evidence = {
        "candidate_source_rows": candidate["source_rows"],
        "candidate_source_files": candidate["source_files"],
        "candidate_windows": candidate["windows"],
        "forward_sessions": len(forward_rows),
        "unadjusted_sessions_with_rows": len(raw_rows),
    }
    if not forward_rows:
        reason = {"reason": "stock_missing_from_forward_daily"}
        return {
            "emotion_rally": empty_emotion_rally(candidate, "data_insufficient", [reason], {
                **base_evidence,
                "data_insufficient_issues": [reason],
            }),
            "all_rallies": [],
        }

    dates = sorted(set(forward_rows) | set(raw_rows))
    first_boards, rejected, insufficient, search_diagnostics = identify_first_boards(
        candidate, dates, raw_rows
    )
    rallies = [track_rally(candidate, board, dates, forward_rows, raw_rows) for board in first_boards]
    rallies.sort(key=lambda rally: rally["first_board_date"])
    selected, selection_rule = select_primary_rally(candidate, rallies)
    selection_evidence = {
        **base_evidence,
        "forward_sessions": len(dates),
        "first_board_rejections": rejected,
        "data_insufficient_issues": insufficient,
        "search_window_diagnostics": search_diagnostics,
        "search_semantics": "cache_start_to_hit_date; no_gain_lookback_limit",
        "selection_rule": selection_rule,
        "available_first_board_dates": [rally["first_board_date"] for rally in rallies],
    }
    if selected is not None:
        primary = dict(selected)
        primary.pop("evaluation_end_date", None)
        primary["evidence"] = {
            **primary["evidence"],
            "candidate_and_selection": selection_evidence,
        }
    elif insufficient:
        primary = empty_emotion_rally(
            candidate, "data_insufficient", [reason_text(item) for item in insufficient], selection_evidence
        )
    else:
        # 缓存可见范围内没有合格首板时，只有一个稳定的业务结论：
        # 可见范围内没有可锚定的首板。搜索是否触达缓存边界另由 diagnostics 记录，
        # 不再把历史搜索边界误写成「首板早于候选 10 日窗口」。
        reason_key = "no_limit_up_in_search_window"
        reasons = [reason_text({"reason": reason_key})]
        primary = empty_emotion_rally(candidate, "rejected", reasons, selection_evidence)

    public_rallies = []
    for rally in rallies:
        public_rally = dict(rally)
        public_rally.pop("evaluation_end_date", None)
        public_rallies.append(public_rally)
    return {"emotion_rally": primary, "all_rallies": public_rallies}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calculate emotion rallies using explicit limit-up status and forward-adjusted OHLC"
    )
    parser.add_argument("--input", required=True, type=Path, help="threshold candidate JSON/CSV file or directory")
    parser.add_argument("--forward-daily", required=True, type=Path, help="forward-adjusted daily JSON/CSV file or directory")
    parser.add_argument("--unadjusted-daily", required=True, type=Path, help="unadjusted daily JSON/CSV with explicit is_limit_up")
    parser.add_argument("--output", required=True, type=Path, help="output JSON file")
    parser.add_argument("--workers", type=int, default=4, help="parallel stock workers")
    args = parser.parse_args()
    if args.workers <= 0:
        parser.error("--workers must be positive")
    args.workers = min(max(args.workers, 1), 16)

    candidates, candidate_meta = load_candidates(args.input)
    candidate_codes = {candidate["code"] for candidate in candidates}
    forward_by_code, forward_meta = load_daily(args.forward_daily, adjusted=True, codes=candidate_codes)
    raw_by_code, raw_meta = load_daily(args.unadjusted_daily, adjusted=False, codes=candidate_codes)
    gain_lookback = int(candidate_meta.get("lookback_trading_sessions") or 10)
    if gain_lookback <= 0:
        raise ValueError("lookback_trading_sessions must be positive")

    stocks: list[dict[str, Any]] = []
    worker_count = max(1, min(args.workers, len(candidates)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(process_stock, candidate, forward_by_code, raw_by_code): candidate["code"]
            for candidate in candidates
        }
        for future in as_completed(futures):
            stocks.append(future.result())
    stocks.sort(key=lambda item: item["emotion_rally"]["code"])
    rallies = [rally for stock in stocks for rally in stock["all_rallies"]]
    rallies.sort(key=lambda item: (item["code"], item["first_board_date"]))

    output = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "parameters_version": PARAMETERS_VERSION,
        "parameters": {
            "workers": args.workers,
            "gain_lookback_trading_sessions": gain_lookback,
            "candidate_window_range": "cache_start_to_hit_date",
            "first_board_search_lookback_constraint": None,
            "first_board_prior_non_limit_sessions": 5,
            "first_board_non_adjacent_prior_check_indices": "i-5..i-2",
            "trend_end_consecutive_no_new_high_sessions": 4,
            "style_limit_up_ratio_thresholds": {"continuous_board_min": 0.5, "trend_max_exclusive": 0.3},
        },
        "metric_definitions": {
            "first_board_date": "Explicit is_limit_up=true on i, with explicit false on i-1 and on i-5..i-2. When a candidate window has base_date, the search range is the closed interval [base_date, hit_date]; when base_date is absent, it is [cache_start, hit_date]. The 10-session gain lookback is not a first-board search limit. Missing required status is data_insufficient; prices/returns are never used to infer status.",
            "rejected_reasons": "no_limit_up_in_search_window = the cache-visible range contains no limit-up day at all, so this move was not built by limit-ups and has no first board to anchor. rejected never means 'data missing'; data missing is always data_insufficient. A search that reaches cache_start is recorded in search_window_diagnostics as a cache boundary, not treated as a lookback failure. Any limit-up chain whose true start is earlier than cache_start remains bounded by the cache and must not be inferred beyond it.",
            "trend_end_date": "Starting at first_board_date, track forward-adjusted high. A new high updates high_date and resets the counter. Four consecutive sessions without a new high confirm the fourth session as trend_end_date, except explicit is_limit_up=true on that fourth session resets the window; missing fourth-day status is data_insufficient.",
            "rally_gain_pct": "100 * (rolling maximum forward-adjusted high / same-scale previous-session close - 1). The unadjusted previous close is audit_baseline_raw. It is used as denominator only when numerically on the same absolute scale as the forward-adjusted previous close; otherwise the forward-adjusted previous close is used. Cross-adjustment division is prohibited.",
            "rally_days": "Trading-session index difference between first_board_date and confirmed trend_end_date; for pending/data_insufficient records, the difference to the latest evaluated session.",
            "style": "Within the evaluated interval, explicit limit-up-day ratio >=50% is 连板型, <30% is 趋势型, otherwise 混合型. Any missing interval limit status makes style unknown.",
            "pending": "No confirmed ending window before available forward data ends, including fewer than four remaining non-new-high sessions after the latest reset.",
        },
        "sources": {
            "candidate": {"path": str(args.input), **candidate_meta},
            "forward_adjusted_daily": {"path": str(args.forward_daily), **forward_meta},
            "unadjusted_daily_and_limit_status": {"path": str(args.unadjusted_daily), **raw_meta},
            "network_access": False,
        },
        "summary": {
            "unique_candidate_stocks": len(candidates),
            "stocks_processed": len(stocks),
            "stocks_with_confirmed_first_board": sum(bool(stock.get("all_rallies")) for stock in stocks),
            "stocks_data_insufficient": sum(stock.get("emotion_rally", {}).get("status") == "data_insufficient" for stock in stocks),
            "rally_records": len(rallies),
            "trend_end_confirmed": sum(rally["trend_end_evidence"] == "confirmed" for rally in rallies),
            "trend_end_pending": sum(rally["trend_end_evidence"] == "pending" for rally in rallies),
            "trend_end_data_insufficient": sum(rally["trend_end_evidence"] == "data_insufficient" for rally in rallies),
        },
        "emotion_rallies": [stock["emotion_rally"] for stock in stocks],
        "rallies": rallies,
        "stocks": stocks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), **output["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
