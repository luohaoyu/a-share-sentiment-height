#!/usr/bin/env python3
"""Validate and merge 10-day, structure-wave, and emotion-rally metrics."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

ALLOWED_STATUSES = {"confirmed", "pending", "data_insufficient", "rejected"}
STRUCTURE_KEYS = ("structure_waves", "results")
EMOTION_KEYS = ("emotion_rallies",)
CODE_KEYS = ("code", "ts_code", "stock_code")
STRUCTURE_GAIN_KEYS = (
    "theoretical_gain_pct",
    "observable_gain_pct",
    "dual_confirm_gain_pct",
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


def normalize_code(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if not value.is_integer():
            return ""
        raw = str(int(value))
    else:
        raw = str(value).strip()
    raw = raw.split(".", 1)[0]
    if not re.fullmatch(r"\d{1,6}", raw):
        return ""
    return raw.zfill(6)


def code_of(row: dict[str, Any]) -> str:
    return normalize_code(first_value(row, CODE_KEYS))


def normalized_dates(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def parse_iso_date(value: Any, label: str, errors: list[str]) -> date | None:
    if value in (None, ""):
        errors.append(f"{label}缺失")
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        errors.append(f"{label}不是有效ISO日期: {value}")
        return None


def candidate_rows(data: Any, keys: tuple[str, ...], label: str, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        errors.append(f"{label}输入顶层必须是对象")
        return []
    rows: Any = None
    for key in keys:
        if key in data:
            rows = data[key]
            break
    if rows is None:
        errors.append(f"{label}缺少候选列表，期望字段之一: {', '.join(keys)}")
        return []
    if not isinstance(rows, list):
        errors.append(f"{label}候选列表必须是数组")
        return []
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"{label}候选[{index}]必须是对象")
            continue
        result.append(row)
    return result


def validate_base(base: Any, errors: list[str], warnings: list[str]) -> dict[str, list[str]]:
    if not isinstance(base, dict):
        errors.append("base顶层必须是对象")
        return {}
    dates = base.get("dates")
    qualified = base.get("qualified")
    if not isinstance(dates, list) or not dates:
        errors.append("base dates必须是非空数组")
        dates = []
    if dates != sorted(set(dates)):
        errors.append("base dates必须升序且不重复")
    for index, value in enumerate(dates):
        try:
            date.fromisoformat(str(value))
        except ValueError:
            errors.append(f"base dates[{index}]不是有效ISO日期: {value}")
    if not isinstance(qualified, list):
        errors.append("base qualified必须是数组")
        qualified = []

    hits: dict[str, list[str]] = defaultdict(list)
    seen_date_codes: set[tuple[str, str]] = set()
    valid_dates = {str(value) for value in dates}
    for index, row in enumerate(qualified):
        if not isinstance(row, dict):
            errors.append(f"base qualified[{index}]必须是对象")
            continue
        raw_code = first_value(row, CODE_KEYS)
        code = normalize_code(raw_code)
        hit_date = str(row.get("date") or "").strip()
        if not code:
            errors.append(f"base qualified[{index}]股票代码无法归一为6位: {raw_code}")
        if not hit_date:
            errors.append(f"base qualified[{index}]缺少date")
        elif hit_date not in valid_dates:
            errors.append(f"base qualified[{index}]日期不在dates中: {hit_date}")
        key = (hit_date, code)
        if code and hit_date and key in seen_date_codes:
            errors.append(f"base qualified存在重复日期/股票: {hit_date} {code}")
        seen_date_codes.add(key)
        if code and hit_date:
            hits[code].append(hit_date)

    declared_rows = base.get("qualified_rows")
    if declared_rows is not None:
        try:
            if int(declared_rows) != len(qualified):
                errors.append("base qualified_rows与qualified长度不一致")
        except (TypeError, ValueError):
            errors.append("base qualified_rows不是整数")

    declared_codes = base.get("unique_qualified_codes")
    if declared_codes is not None:
        if not isinstance(declared_codes, list):
            errors.append("base unique_qualified_codes必须是数组")
        else:
            normalized_declared = {normalize_code(item) for item in declared_codes}
            if "" in normalized_declared:
                errors.append("base unique_qualified_codes包含无法归一为6位的代码")
            normalized_declared.discard("")
            if normalized_declared != set(hits):
                errors.append("base unique_qualified_codes与qualified按6位归一后的去重代码集合不一致")

    prior_valid = base.get("validation", {}).get("valid") is True if isinstance(base.get("validation"), dict) else False
    if not prior_valid and not errors:
        warnings.append("base validation.valid不为true；已重新验证基本dates/qualified并通过")
    return {code: sorted(set(values)) for code, values in hits.items()}


def legacy_reasons(row: dict[str, Any]) -> list[str]:
    evidence = row.get("evidence")
    if not isinstance(evidence, dict):
        return []
    reasons: list[str] = []
    reason = evidence.get("reason")
    if isinstance(reason, str) and reason.strip():
        reasons.append(reason.strip())
    issues = evidence.get("issues")
    if isinstance(issues, list):
        for issue in issues:
            value = issue.get("reason") if isinstance(issue, dict) else issue
            if isinstance(value, str) and value.strip():
                reasons.append(value.strip())
    return list(dict.fromkeys(reasons))


def status_of_structure(row: dict[str, Any]) -> Any:
    return row.get("status")


def status_of_emotion(row: dict[str, Any]) -> Any:
    return row.get("status", row.get("trend_end_evidence"))


def validate_common_rows(
    rows: list[dict[str, Any]],
    expected_hits: dict[str, list[str]],
    label: str,
    status_getter: Callable[[dict[str, Any]], Any],
    errors: list[str],
    warnings: list[str],
) -> Counter[str]:
    codes: list[str] = []
    statuses: Counter[str] = Counter()
    for index, row in enumerate(rows):
        raw_code = first_value(row, CODE_KEYS)
        code = normalize_code(raw_code)
        prefix = f"{label}候选[{index}]" + (f"({code})" if code else "")
        if not code:
            errors.append(f"{prefix}股票代码无法归一为6位: {raw_code}")
        codes.append(code)
        status = status_getter(row)
        if status not in ALLOWED_STATUSES:
            errors.append(f"{prefix}状态非法: {status}")
        else:
            statuses[str(status)] += 1
        hit_dates = row.get("hit_dates")
        if not isinstance(hit_dates, list):
            errors.append(f"{prefix} hit_dates必须是数组")
        elif hit_dates != normalized_dates(hit_dates):
            errors.append(f"{prefix} hit_dates必须排序且去重")
        elif code and normalized_dates(hit_dates) != expected_hits.get(code, []):
            errors.append(f"{prefix} hit_dates与base按6位代码归一后的全部命中日期不一致")
        if status == "data_insufficient":
            reasons = row.get("reasons")
            if reasons is None:
                reasons = legacy_reasons(row)
                if reasons:
                    row["reasons"] = reasons
                    warnings.append(f"{prefix}已从旧版evidence.reason/evidence.issues标准化顶层reasons；建议计算脚本直接输出reasons")
            if not isinstance(reasons, list) or not reasons or any(not isinstance(item, str) or not item.strip() for item in reasons):
                errors.append(f"{prefix} data_insufficient必须提供非空顶层reasons字符串数组")
    duplicates = sorted(code for code, count in Counter(codes).items() if code and count > 1)
    if duplicates:
        errors.append(f"{label}存在重复股票: {duplicates}")
    actual_codes = {code for code in codes if code}
    expected_codes = set(expected_hits)
    if actual_codes != expected_codes:
        missing = sorted(expected_codes - actual_codes)
        extra = sorted(actual_codes - expected_codes)
        errors.append(f"{label}候选代码集合与base按6位归一后不一致；缺少={missing}；多出={extra}")
    return statuses


def validate_finite_field(row: dict[str, Any], key: str, prefix: str, errors: list[str]) -> None:
    value = row.get(key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append(f"{prefix} {key}必须是有限数值")
        return
    if not math.isfinite(number):
        errors.append(f"{prefix} {key}必须是有限数值")


def validate_structure(rows: list[dict[str, Any]], errors: list[str]) -> None:
    for index, row in enumerate(rows):
        code = code_of(row)
        prefix = f"结构候选[{index}]({code})"
        status = status_of_structure(row)
        if status == "pending" and row.get("end_date") is not None:
            errors.append(f"{prefix} pending的end_date必须为null")
        if status != "confirmed":
            continue
        start = parse_iso_date(row.get("start_date"), f"{prefix} start_date", errors)
        start_confirm = parse_iso_date(row.get("start_confirm_date"), f"{prefix} start_confirm_date", errors)
        measurement = parse_iso_date(row.get("measurement_date"), f"{prefix} measurement_date", errors)
        if start and start_confirm and measurement and not (start <= start_confirm <= measurement):
            errors.append(
                f"{prefix}日期顺序必须满足 start_date <= start_confirm_date <= measurement_date"
            )
        if row.get("end_date") is not None or row.get("end_confirm_date") is not None:
            end_raw = row.get("end_date")
            end_confirm_raw = row.get("end_confirm_date")
            if end_raw is None or end_confirm_raw is None:
                errors.append(f"{prefix} end_date 与 end_confirm_date 必须同时存在或同时为null")
            else:
                end = parse_iso_date(end_raw, f"{prefix} end_date", errors)
                end_confirm = parse_iso_date(end_confirm_raw, f"{prefix} end_confirm_date", errors)
                if start and end and not (start <= end):
                    errors.append(f"{prefix}终点必须晚于或等于起点: start_date <= end_date")
                if end and end_confirm and end > end_confirm:
                    errors.append(f"{prefix}终点日期顺序必须满足 end_date <= end_confirm_date")
                if end_confirm and measurement and end_confirm > measurement:
                    errors.append(f"{prefix} end_confirm_date不得晚于measurement_date（禁止前视偏差）")
            if row.get("end_high") is None:
                errors.append(f"{prefix}存在end_date时必须提供end_high")
        for key in STRUCTURE_GAIN_KEYS:
            value = row.get(key)
            if value is None:
                # 双端确认收益只在该波段确有已确认顶分型时才成立，缺失须为null而非顶替
                if key == "dual_confirm_gain_pct" and row.get("end_date") is None:
                    continue
                errors.append(f"{prefix} {key}缺失或为null")
                continue
            validate_finite_field(row, key, prefix, errors)


def validate_emotion(rows: list[dict[str, Any]], errors: list[str]) -> None:
    for index, row in enumerate(rows):
        code = code_of(row)
        prefix = f"情绪候选[{index}]({code})"
        status = status_of_emotion(row)
        if status == "pending" and row.get("trend_end_date") is not None:
            errors.append(f"{prefix} pending的trend_end_date必须为null")
        if status != "confirmed":
            continue
        first_board = parse_iso_date(row.get("first_board_date"), f"{prefix} first_board_date", errors)
        trend_end = parse_iso_date(row.get("trend_end_date"), f"{prefix} trend_end_date", errors)
        if first_board and trend_end and first_board > trend_end:
            errors.append(f"{prefix}日期顺序必须满足 first_board_date <= trend_end_date")
        validate_finite_field(row, "rally_gain_pct", prefix, errors)


def status_statistics(counter: Counter[str]) -> dict[str, int]:
    return {status: counter.get(status, 0) for status in sorted(ALLOWED_STATUSES)}


def metric_contract(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"parameters_version": None, "metric_definitions": None}
    return {
        "parameters_version": data.get("parameters_version"),
        "metric_definitions": data.get("metric_definitions"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="校验并合并三类情绪高度指标JSON")
    parser.add_argument("--base-input", required=True, type=Path, help="原10日情绪高度JSON")
    parser.add_argument("--structure-input", required=True, type=Path, help="缠论结构波段JSON")
    parser.add_argument("--emotion-input", required=True, type=Path, help="首板情绪波段JSON")
    parser.add_argument("--output", required=True, type=Path, help="合并后的最终JSON；无默认原地覆盖")
    args = parser.parse_args()

    base = load_json(args.base_input)
    structure = load_json(args.structure_input)
    emotion = load_json(args.emotion_input)
    errors: list[str] = []
    warnings: list[str] = []

    expected_hits = validate_base(base, errors, warnings)
    structure_rows = candidate_rows(structure, STRUCTURE_KEYS, "结构", errors)
    emotion_rows = candidate_rows(emotion, EMOTION_KEYS, "情绪", errors)
    structure_statuses = validate_common_rows(
        structure_rows, expected_hits, "结构", status_of_structure, errors, warnings
    )
    emotion_statuses = validate_common_rows(
        emotion_rows, expected_hits, "情绪", status_of_emotion, errors, warnings
    )
    validate_structure(structure_rows, errors)
    validate_emotion(emotion_rows, errors)

    total = len(expected_hits)
    structure_covered = sum(count for status, count in structure_statuses.items() if status != "data_insufficient")
    emotion_covered = sum(count for status, count in emotion_statuses.items() if status != "data_insufficient")
    coverage = {
        "base_candidate_stocks": total,
        "structure_candidate_stocks": len(structure_rows),
        "emotion_candidate_stocks": len(emotion_rows),
        "structure_covered_stocks": structure_covered,
        "emotion_covered_stocks": emotion_covered,
        "structure_coverage_ratio": round(structure_covered / total, 6) if total else 1.0,
        "emotion_coverage_ratio": round(emotion_covered / total, 6) if total else 1.0,
    }
    validation = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "coverage": coverage,
    }
    merged = dict(base) if isinstance(base, dict) else {}
    merged.update({
        "schema_version": "three-height-metrics-v1",
        "height_metrics": {
            "ten_day_return": {
                "definition": "每个交易日合格股票的10个交易日区间收益；候选股票为观察期内达到base阈值的去重股票集合",
                "unit": "percentage_points；50表示50%",
                "status_counts": {"confirmed": total, "pending": 0, "data_insufficient": 0, "rejected": 0},
            },
            "structure_wave": {
                "definition": "包含处理后的底分型起点涨幅；theoretical_gain_pct为measurement_close/start_low-1，observable_gain_pct为measurement_close/fractal_confirm_close-1；只做顶底分型，不构造笔、线段或中枢",
                "unit": "decimal_return；0.5表示50%（遵循结构计算脚本metric_definitions）",
                "status_counts": status_statistics(structure_statuses),
            },
            "emotion_rally": {
                "definition": "首板日起至情绪趋势结束日的波段高度，rally_gain_pct按同尺度前收盘价至区间最高价计算",
                "unit": "percentage_points；50表示50%",
                "status_counts": status_statistics(emotion_statuses),
            },
        },
        "metric_contracts": {
            "structure_wave": metric_contract(structure),
            "emotion_rally": metric_contract(emotion),
        },
        "structure_waves": structure_rows,
        "emotion_rallies": emotion_rows,
        "three_height_validation": validation,
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
