#!/usr/bin/env python3
"""Calculate daily structure waves for threshold-qualified stocks without network access."""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

PARAMETERS_VERSION = "fractal-structure-v2.0.0"
MARKET_GROUPS = {
    "沪深主板": {"沪市主板", "深市主板"},
    "科创成长板": {"科创板", "创业板"},
}

CODE_KEYS = ("ts_code", "code", "symbol", "stock_code", "security_code", "股票代码", "证券代码", "代码")
NAME_KEYS = ("name", "stock_name", "sec_name", "security_name", "股票名称", "证券名称", "证券简称", "名称")
MARKET_KEYS = ("market", "market_label", "market_source", "board", "市场", "市场名称", "板块")
GROUP_KEYS = ("market_group", "group", "市场分组", "组合市场")
DATE_KEYS = ("trade_date", "date", "cal_date", "datetime", "交易日期", "日期", "时间")
OPEN_KEYS = ("open", "adj_open", "qfq_open", "开盘", "开盘价", "前复权开盘")
HIGH_KEYS = ("high", "adj_high", "qfq_high", "最高", "最高价", "前复权最高")
LOW_KEYS = ("low", "adj_low", "qfq_low", "最低", "最低价", "前复权最低")
CLOSE_KEYS = ("close", "adj_close", "qfq_close", "收盘", "收盘价", "前复权收盘")
HIT_DATE_KEYS = ("hit_date", "threshold_date", "命中日期", "达标日期")


def first_value(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if match:
        return match.group(1)
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return text


def normalize_date(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-").replace(".", "-")
    if not text:
        raise ValueError("empty trade date")
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    elif len(text) >= 10:
        text = text[:10]
    return date.fromisoformat(text).isoformat()


def maybe_date(value: Any) -> str | None:
    try:
        return normalize_date(value)
    except (TypeError, ValueError):
        return None


def finite_number(value: Any, field: str) -> float:
    if isinstance(value, str):
        value = value.strip().replace(",", "")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite {field}: {value!r}")
    return number


def infer_market(code: str) -> str | None:
    if code.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if code.startswith(("000", "001", "002", "003")):
        return "深市主板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith("688"):
        return "科创板"
    return None


def normalize_market(value: Any, code: str) -> str | None:
    text = str(value or "").strip()
    upper = text.upper()
    if "科创" in text or "STAR" in upper:
        return "科创板"
    if "创业" in text or "CHINEXT" in upper:
        return "创业板"
    if "沪" in text or "SHANGHAI" in upper or upper in {"SH", "SSE", "XSHG"}:
        return "沪市主板"
    if "深" in text or "SHENZHEN" in upper or upper in {"SZ", "SZSE", "XSHE"}:
        return "深市主板"
    return text or infer_market(code)


def market_group_for(market: str | None) -> str | None:
    for group, markets in MARKET_GROUPS.items():
        if market in markets:
            return group
    return None


def candidate_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("qualified"), list):
        raise ValueError("candidate JSON must contain a 'qualified' array")
    return [row for row in payload["qualified"] if isinstance(row, dict)]


def collect_hit_dates(row: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    raw_dates = row.get("hit_dates")
    if isinstance(raw_dates, list):
        values.extend(raw_dates)
    elif raw_dates not in (None, ""):
        values.extend(part.strip() for part in str(raw_dates).split(",") if part.strip())
    for key in ("date", "trade_date", "cal_date", "交易日期", "日期", *HIT_DATE_KEYS):
        if row.get(key) not in (None, ""):
            values.append(row[key])
    return {normalized for value in values if (normalized := maybe_date(value)) is not None}


def load_candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    merged: dict[str, dict[str, Any]] = {}
    for row in candidate_rows(payload):
        raw_code = first_value(row, CODE_KEYS)
        code = normalize_code(raw_code)
        if not code:
            raise ValueError(f"qualified row is missing code: {row!r}")
        item = merged.setdefault(code, {
            "code": code,
            "ts_code": str(raw_code).strip(),
            "name": "",
            "market": None,
            "market_group": None,
            "hit_dates": set(),
        })
        name = first_value(row, NAME_KEYS)
        market = normalize_market(first_value(row, MARKET_KEYS), code)
        group = first_value(row, GROUP_KEYS)
        if str(raw_code).strip() and "." in str(raw_code):
            item["ts_code"] = str(raw_code).strip()
        if name not in (None, ""):
            item["name"] = str(name).strip()
        if market:
            item["market"] = market
        if group not in (None, ""):
            item["market_group"] = str(group).strip()
        item["hit_dates"].update(collect_hit_dates(row))
    result = []
    for code in sorted(merged):
        item = merged[code]
        item["name"] = item["name"] or code
        item["market"] = item["market"] or infer_market(code)
        item["market_group"] = item["market_group"] or market_group_for(item["market"])
        item["hit_dates"] = sorted(item["hit_dates"])
        result.append(item)
    return result


def input_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() not in {".json", ".csv"}:
            raise ValueError(f"unsupported daily file type: {path}")
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)
    files = sorted(
        item for item in path.rglob("*")
        if item.is_file() and item.suffix.lower() in {".json", ".csv"}
    )
    if not files:
        raise ValueError(f"no JSON/CSV daily files found under {path}")
    return files


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except UnicodeDecodeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def records_from_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    data = value.get("data")
    if isinstance(data, dict) and isinstance(data.get("fields"), list) and isinstance(data.get("items"), list):
        fields = data["fields"]
        return [dict(zip(fields, row)) for row in data["items"] if isinstance(row, list)]
    for key in ("rows", "items", "records", "result", "data", "daily", "prices", "ohlc"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [dict(row) for row in nested if isinstance(row, dict)]
        if isinstance(nested, dict):
            records = records_from_json(nested)
            if records:
                return records
    records: list[dict[str, Any]] = []
    for key, nested in value.items():
        if not isinstance(nested, list):
            continue
        for row in nested:
            if not isinstance(row, dict):
                continue
            copy = dict(row)
            if normalize_code(key) != key or re.search(r"\d{6}", str(key)):
                copy.setdefault("code", key)
            elif maybe_date(key):
                copy.setdefault("date", key)
            records.append(copy)
    return records


def fallback_code_from_path(path: Path) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", path.stem)
    return match.group(1) if match else ""


def fallback_date_from_path(path: Path) -> str | None:
    match = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", path.stem)
    return maybe_date("".join(match.groups())) if match else None


def load_daily(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    by_code_date: dict[tuple[str, str], dict[str, Any]] = {}
    quality: dict[str, Any] = {
        "files": 0,
        "source_rows": 0,
        "accepted_rows": 0,
        "skipped_rows": 0,
        "duplicate_rows": 0,
        "skipped_rows_by_code": {},
    }
    for file in input_files(path):
        quality["files"] += 1
        rows = read_csv_rows(file) if file.suffix.lower() == ".csv" else records_from_json(
            json.loads(file.read_text(encoding="utf-8-sig"))
        )
        fallback_code = fallback_code_from_path(file)
        fallback_date = fallback_date_from_path(file)
        for row in rows:
            quality["source_rows"] += 1
            raw_code = first_value(row, CODE_KEYS) or fallback_code
            code = normalize_code(raw_code)
            try:
                raw_date = first_value(row, DATE_KEYS) or fallback_date
                if not code or raw_date in (None, ""):
                    raise ValueError("missing code or trade date")
                normalized = {
                    "code": code,
                    "date": normalize_date(raw_date),
                    "open": finite_number(first_value(row, OPEN_KEYS), "open"),
                    "high": finite_number(first_value(row, HIGH_KEYS), "high"),
                    "low": finite_number(first_value(row, LOW_KEYS), "low"),
                    "close": finite_number(first_value(row, CLOSE_KEYS), "close"),
                }
                if any(normalized[field] <= 0 for field in ("open", "high", "low", "close")):
                    raise ValueError("OHLC values must be positive")
                if normalized["high"] < normalized["low"]:
                    raise ValueError("high is below low")
            except (TypeError, ValueError):
                quality["skipped_rows"] += 1
                if code:
                    skipped_by_code = quality["skipped_rows_by_code"]
                    skipped_by_code[code] = skipped_by_code.get(code, 0) + 1
                continue
            key = (code, normalized["date"])
            previous = by_code_date.get(key)
            if previous is not None:
                price_fields = ("open", "high", "low", "close")
                if any(not math.isclose(previous[field], normalized[field], rel_tol=1e-12, abs_tol=1e-12) for field in price_fields):
                    raise ValueError(f"conflicting duplicate OHLC row for {code} on {normalized['date']}")
                quality["duplicate_rows"] += 1
                continue
            by_code_date[key] = normalized
            quality["accepted_rows"] += 1
    grouped: dict[str, list[dict[str, Any]]] = {}
    for (code, _), row in by_code_date.items():
        grouped.setdefault(code, []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: item["date"])
    return grouped, quality


def moving_average_5(rows: list[dict[str, Any]]) -> list[float | None]:
    values: list[float | None] = [None] * len(rows)
    rolling = 0.0
    for index, row in enumerate(rows):
        rolling += row["close"]
        if index >= 5:
            rolling -= rows[index - 5]["close"]
        if index >= 4:
            values[index] = rolling / 5.0
    return values


def bottom_fractal(rows: list[dict[str, Any]], index: int) -> bool:
    previous, current, following = rows[index - 1], rows[index], rows[index + 1]
    return (
        current["low"] < previous["low"]
        and current["low"] < following["low"]
        and current["high"] < previous["high"]
        and current["high"] < following["high"]
    )


def top_fractal(rows: list[dict[str, Any]], index: int) -> bool:
    previous, current, following = rows[index - 1], rows[index], rows[index + 1]
    return (
        current["high"] > previous["high"]
        and current["high"] > following["high"]
        and current["low"] > previous["low"]
        and current["low"] > following["low"]
    )


def bullish_confirmation(rows: list[dict[str, Any]], ma5: list[float | None], second: int) -> bool:
    first = second - 1
    return (
        ma5[first] is not None
        and ma5[second] is not None
        and rows[first]["close"] > ma5[first]
        and rows[second]["close"] > ma5[second]
        and ma5[second] > ma5[first]
    )


def bearish_confirmation(rows: list[dict[str, Any]], ma5: list[float | None], second: int) -> bool:
    first = second - 1
    return (
        ma5[first] is not None
        and ma5[second] is not None
        and rows[first]["close"] < ma5[first]
        and rows[second]["close"] < ma5[second]
        and ma5[second] < ma5[first]
    )


def compact_bar(row: dict[str, Any], ma5: float | None = None) -> dict[str, Any]:
    result = {key: row[key] for key in ("date", "open", "high", "low", "close")}
    if ma5 is not None:
        result["ma5"] = round(ma5, 10)
    return result


def fractal_evidence(rows: list[dict[str, Any]], index: int, kind: str) -> dict[str, Any]:
    return {
        "type": kind,
        "previous": compact_bar(rows[index - 1]),
        "middle": compact_bar(rows[index]),
        "next": compact_bar(rows[index + 1]),
    }


def confirmation_evidence(
    rows: list[dict[str, Any]], ma5: list[float | None], second: int, direction: str
) -> dict[str, Any]:
    first = second - 1
    return {
        "direction": direction,
        "first_day": compact_bar(rows[first], ma5[first]),
        "second_day": compact_bar(rows[second], ma5[second]),
        "close_relation": "close>MA5" if direction == "up" else "close<MA5",
        "ma5_relation": "second_ma5>first_ma5" if direction == "up" else "second_ma5<first_ma5",
    }


def base_result(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": candidate["code"],
        "ts_code": candidate["ts_code"],
        "name": candidate["name"],
        "market": candidate["market"],
        "market_group": candidate["market_group"],
        "hit_dates": candidate["hit_dates"],
        "status": "rejected",
        "reasons": [],
        "start_date": None,
        "start_confirm_date": None,
        "start_low": None,
        "theoretical_gain_pct": None,
        "observable_gain_pct": None,
        "dual_confirm_gain_pct": None,
        "bars_count": 0,
        "evidence": {},
        "parameters_version": PARAMETERS_VERSION,
    }


def start_evidence(
    rows: list[dict[str, Any]], ma5: list[float | None], start_index: int, confirm_index: int
) -> dict[str, Any]:
    return {
        "fractal": fractal_evidence(rows, start_index, "strict_bottom"),
        "confirmation": confirmation_evidence(rows, ma5, confirm_index, "up"),
    }


def has_inclusion(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """相同高低点不合并；其余一方区间覆盖另一方时视为包含。"""
    if math.isclose(left["high"], right["high"]) and math.isclose(left["low"], right["low"]):
        return False
    return (
        (left["high"] >= right["high"] and left["low"] <= right["low"])
        or (right["high"] >= left["high"] and right["low"] <= left["low"])
    )


def merge_inclusions(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按时间顺序迭代消除包含关系，并保留原始日期映射与极值来源。"""
    merged: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for source in rows:
        bar = dict(source)
        bar["source_dates"] = [source["date"]]
        bar["high_source_date"] = source["date"]
        bar["low_source_date"] = source["date"]
        if not merged or not has_inclusion(merged[-1], bar):
            merged.append(bar)
            continue

        previous = merged[-1]
        if len(merged) >= 2:
            anchor = merged[-2]
            direction = "up" if previous["high"] > anchor["high"] else "down"
        else:
            direction = "up" if bar["high"] > previous["high"] else "down"

        if direction == "up":
            new_high = max(previous["high"], bar["high"])
            new_low = max(previous["low"], bar["low"])
        else:
            new_high = min(previous["high"], bar["high"])
            new_low = min(previous["low"], bar["low"])

        high_source = previous["high_source_date"] if math.isclose(new_high, previous["high"]) else bar["high_source_date"]
        low_source = previous["low_source_date"] if math.isclose(new_low, previous["low"]) else bar["low_source_date"]
        combined = {
            "date": bar["date"],
            "open": previous["open"],
            "high": new_high,
            "low": new_low,
            "close": bar["close"],
            "source_dates": previous["source_dates"] + bar["source_dates"],
            "high_source_date": high_source,
            "low_source_date": low_source,
        }
        records.append({
            "direction": direction,
            "left_dates": previous["source_dates"],
            "right_dates": bar["source_dates"],
            "result_date": combined["date"],
            "result_high": new_high,
            "result_low": new_low,
        })
        merged[-1] = combined
    return merged, records


def identify_fractals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for index in range(1, len(rows) - 1):
        kind = None
        if bottom_fractal(rows, index):
            kind = "bottom"
        elif top_fractal(rows, index):
            kind = "top"
        if kind:
            current = rows[index]
            found.append({
                "type": kind,
                "merged_index": index,
                "fractal_date": current["date"],
                "confirm_date": rows[index + 1]["date"],
                "extreme_date": current["low_source_date"] if kind == "bottom" else current["high_source_date"],
                "extreme_price": current["low"] if kind == "bottom" else current["high"],
                "source_dates": current["source_dates"],
                "previous": compact_bar(rows[index - 1]),
                "middle": compact_bar(current),
                "next": compact_bar(rows[index + 1]),
            })

    deduplicated: list[dict[str, Any]] = []
    for item in found:
        if not deduplicated or deduplicated[-1]["type"] != item["type"]:
            deduplicated.append(item)
            continue
        previous = deduplicated[-1]
        more_extreme = (
            item["extreme_price"] <= previous["extreme_price"]
            if item["type"] == "bottom"
            else item["extreme_price"] >= previous["extreme_price"]
        )
        if more_extreme:
            item["supersedes"] = previous["fractal_date"]
            deduplicated[-1] = item
    return deduplicated


def calculate_one(
    candidate: dict[str, Any], rows: list[dict[str, Any]], invalid_rows: int = 0
) -> dict[str, Any]:
    result = base_result(candidate)
    evidence: dict[str, Any] = {
        "data_bars": len(rows),
        "invalid_or_incomplete_rows": invalid_rows,
        "first_trade_date": rows[0]["date"] if rows else None,
        "last_trade_date": rows[-1]["date"] if rows else None,
        "ordering": "trade_date_ascending",
        "method": "inclusion_merge_then_top_bottom_fractals_only",
    }
    result["evidence"] = evidence
    if len(rows) < 3 or invalid_rows:
        reasons = []
        if len(rows) < 3:
            reasons.append("至少需要3根完整OHLC日线，才能形成顶底分型")
        if invalid_rows:
            reasons.append(f"存在{invalid_rows}根字段缺失或无效的日线，无法保证交易日序列完整")
        result.update({"status": "data_insufficient", "reasons": reasons})
        evidence["reason"] = "；".join(reasons)
        return result

    merged, inclusion_records = merge_inclusions(rows)
    fractals = identify_fractals(merged)
    latest_hit_date = max(candidate["hit_dates"], default=rows[-1]["date"])
    close_by_date = {row["date"]: row["close"] for row in rows}
    measurement_dates = [day for day in close_by_date if day <= latest_hit_date]
    measurement_date = max(measurement_dates, default=None)
    bottoms = [item for item in fractals if item["type"] == "bottom" and item["confirm_date"] <= latest_hit_date]
    evidence.update({
        "merged_bars": len(merged),
        "inclusion_merge_count": len(inclusion_records),
        "inclusion_merges": inclusion_records,
        "fractals": fractals,
        "confirmed_bottom_candidates": len(bottoms),
        "latest_hit_date": latest_hit_date,
        "measurement_date": measurement_date,
        "selection": "latest confirmed bottom fractal on or before latest hit date",
    })

    if not bottoms or measurement_date is None:
        reason = "数据完整，但latest hit date之前没有已确认底分型起点"
        result.update({"status": "rejected", "reasons": [reason]})
        evidence["reason"] = reason
        return result

    selected = max(bottoms, key=lambda item: (item["confirm_date"], item["fractal_date"]))
    confirm_close = close_by_date.get(selected["confirm_date"])
    measurement_close = close_by_date[measurement_date]
    theoretical = measurement_close / selected["extreme_price"] - 1.0
    observable = measurement_close / confirm_close - 1.0 if confirm_close else None

    # 终点（顶分型）：只认观察期内已确认的顶分型，避免前视偏差。
    # 约束 confirm_date > 起点确认日，且 confirm_date <= 测量日（达标日），
    # 取满足条件中最近的一个；没有则终点三字段留空、双端确认收益留空，
    # 绝不用 observable 顶替（旧实现如此，导致"双端确认"与"确认后高度"恒等）。
    tops_after_start = [
        item
        for item in fractals
        if item["type"] == "top"
        and selected["confirm_date"] < item["confirm_date"] <= measurement_date
        and item["fractal_date"] >= selected["fractal_date"]
    ]
    end_fractal = (
        max(tops_after_start, key=lambda item: (item["confirm_date"], item["fractal_date"]))
        if tops_after_start
        else None
    )
    end_confirm_close = close_by_date.get(end_fractal["confirm_date"]) if end_fractal else None
    dual = (
        end_confirm_close / confirm_close - 1.0
        if end_fractal is not None and confirm_close and end_confirm_close is not None
        else None
    )
    result.update({
        "status": "confirmed",
        "start_date": selected["fractal_date"],
        "start_confirm_date": selected["confirm_date"],
        "start_extreme_date": selected["extreme_date"],
        "start_low": selected["extreme_price"],
        "measurement_date": measurement_date,
        "measurement_close": measurement_close,
        "end_date": end_fractal["fractal_date"] if end_fractal else None,
        "end_confirm_date": end_fractal["confirm_date"] if end_fractal else None,
        "end_high": end_fractal["extreme_price"] if end_fractal else None,
        "theoretical_gain_pct": round(theoretical, 10),
        "observable_gain_pct": round(observable, 10) if observable is not None else None,
        "dual_confirm_gain_pct": round(dual, 10) if dual is not None else None,
        "bars_count": sum(1 for row in rows if selected["extreme_date"] <= row["date"] <= measurement_date),
    })
    evidence["selected_bottom"] = selected
    evidence["selected_top"] = end_fractal
    evidence["end_selection"] = {
        "rule": "latest confirmed top fractal with confirm_date in (start_confirm_date, measurement_date]",
        "candidates": len(tops_after_start),
        "found": end_fractal is not None,
    }
    evidence["gain_inputs"] = {
        "start_low": selected["extreme_price"],
        "confirm_close": confirm_close,
        "measurement_close": measurement_close,
        "end_confirm_close": end_confirm_close,
    }
    return result


def workers_value(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be an integer from 1 to 16") from exc
    if not 1 <= workers <= 16:
        raise argparse.ArgumentTypeError("workers must be from 1 to 16")
    return workers


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate confirmed daily structure waves for qualified stocks")
    parser.add_argument("--input", required=True, type=Path, help="threshold-candidate JSON containing qualified")
    parser.add_argument("--forward-daily", required=True, type=Path, help="forward-adjusted OHLC JSON/CSV file or directory")
    parser.add_argument("--output", required=True, type=Path, help="output JSON path")
    parser.add_argument("--workers", type=workers_value, default=4, help="stock-level worker threads, 1..16 (default: 4)")
    args = parser.parse_args()

    candidates = load_candidates(args.input)
    daily_by_code, daily_quality = load_daily(args.forward_daily)
    indexed_results: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(calculate_one, candidate, daily_by_code.get(candidate["code"], [])): index
            for index, candidate in enumerate(candidates)
        }
        for future in as_completed(futures):
            indexed_results[futures[future]] = future.result()
    results = [indexed_results[index] for index in range(len(candidates))]

    status_counts: dict[str, int] = {}
    for row in results:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    output = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "parameters_version": PARAMETERS_VERSION,
        "parameters": {
            "price_adjustment": "forward_adjusted/qfq input supplied by caller",
            "inclusion_processing": "chronological_iterative_merge",
            "inclusion_equal_range": "not_merged",
            "fractal_strict": True,
            "same_type_deduplication": "bottom_keep_lower_or_later_equal; top_keep_higher_or_later_equal",
            "confirmation": "right_merged_bar_close",
            "workers": args.workers,
            "start_selection": "latest confirmed bottom fractal on or before latest hit date",
            "end_selection": "latest confirmed top fractal with confirm_date in (start_confirm_date, measurement_date]",
            "network_access": False,
        },
        "metric_definitions": {
            "theoretical_gain_pct": {
                "formula": "measurement_close / bottom_fractal_extreme_low - 1",
                "unit": "decimal_return",
                "note": "measurement_date为latest hit date可用收盘日；只表达底分型起点涨幅，不代表完整波段。",
            },
            "observable_gain_pct": {
                "formula": "measurement_close / fractal_confirm_close - 1",
                "unit": "decimal_return",
                "note": "从右侧合并K线收盘确认底分型后计算的可观察涨幅。",
            },
            "dual_confirm_gain_pct": {
                "formula": "top_fractal_confirm_close / bottom_fractal_confirm_close - 1",
                "unit": "decimal_return",
                "note": "起止两端都用合并K线右侧确认后的收盘价，才计入；观察期内没有已确认顶分型时为 null，不得用 observable_gain_pct 顶替。",
            },
            "bars_count": {
                "definition": "从真实极低点日期到measurement_date（含首尾）的原始交易日线数量"
            },
        },
        "source": {
            "input": str(args.input),
            "forward_daily": str(args.forward_daily),
        },
        "quality": {
            "candidate_rows_after_deduplication": len(candidates),
            "daily": daily_quality,
            "status_counts": status_counts,
        },
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "stocks": len(results),
        "status_counts": status_counts,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
