#!/usr/bin/env python3
"""Compute daily market-segment winners from bulk A-share daily snapshots.

The script is intentionally provider-neutral. It consumes normalized CSV/JSON files
exported from a market data service and performs all joins, adjustment, filtering,
10-session return calculation, and ranking locally.

Required logical datasets:
- calendar: cal_date/trade_date/date, is_open
- daily: ts_code/code, trade_date/date, close
- stock basic: ts_code/code, name, list_date/listing_date
- adjustment factors unless --prices-adjusted: ts_code/code, trade_date/date,
  adj_factor
Optional:
- historical ST snapshots: ts_code/code, trade_date/date, name/type/type_name
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

MARKETS = ["沪市主板", "深市主板", "创业板", "科创板", "北交所"]
DEFAULT_TREND_MARKETS = ["沪市主板", "深市主板"]
GAIN_BANDS = [
    {"key": "0-30", "label": "0%—30%", "min": 0, "max": 30, "color": "#E6F4EA", "stroke": "#188038"},
    {"key": "30-50", "label": "30%—50%", "min": 30, "max": 50, "color": "#E8F0FE", "stroke": "#1967D2"},
    {"key": "50-100", "label": "50%—100%", "min": 50, "max": 100, "color": "#FEF7E0", "stroke": "#B06000"},
    {"key": "100-200", "label": "100%—200%", "min": 100, "max": 200, "color": "#FCE8E6", "stroke": "#C5221F"},
    {"key": "200-300", "label": "200%—300%", "min": 200, "max": 300, "color": "#FAD2CF", "stroke": "#A50E0E"},
    {"key": "300-plus", "label": "300%+", "min": 300, "max": None, "color": "#F4CCCC", "stroke": "#85200C"},
]


def first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_date(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) >= 10:
        text = text[:10]
    return date.fromisoformat(text).isoformat()


def normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        symbol, suffix = text.split(".", 1)
        suffix = {"XSHG": "SH", "XSHE": "SZ", "BSE": "BJ"}.get(suffix, suffix)
        return f"{symbol.zfill(6)}.{suffix}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return text
    symbol = digits.zfill(6)
    if symbol.startswith(("600", "601", "603", "605", "688")):
        return f"{symbol}.SH"
    if symbol.startswith(("000", "001", "002", "003", "300")):
        return f"{symbol}.SZ"
    if symbol.startswith(("43", "83", "87", "92")):
        return f"{symbol}.BJ"
    return symbol


def numeric(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite {field}: {value!r}")
    return number


def records_from_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    data = value.get("data")
    if isinstance(data, dict) and isinstance(data.get("fields"), list) and isinstance(data.get("items"), list):
        fields = data["fields"]
        return [dict(zip(fields, item)) for item in data["items"] if isinstance(item, list)]
    for key in ("rows", "items", "records", "result", "data"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
    return []


def input_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)
    return sorted([item for item in path.iterdir() if item.is_file() and item.suffix.lower() in {".json", ".csv"}])


def read_records(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in input_files(path):
        if file.suffix.lower() == ".csv":
            with file.open("r", encoding="utf-8-sig", newline="") as handle:
                rows.extend(dict(row) for row in csv.DictReader(handle))
        else:
            rows.extend(records_from_json(json.loads(file.read_text(encoding="utf-8-sig"))))
    return rows


def market_for(code: str, master: dict[str, Any]) -> str | None:
    symbol = code.split(".", 1)[0]
    suffix = code.split(".", 1)[1] if "." in code else str(master.get("exchange", "")).upper()
    official_market = str(first_value(master, "market", "market_label") or "")
    if suffix in {"BJ", "BSE"} or "北交" in official_market:
        return "北交所"
    if "创业" in official_market or symbol.startswith("300"):
        return "创业板"
    if "科创" in official_market or symbol.startswith("688"):
        return "科创板"
    if suffix in {"SH", "SSE"} and symbol.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if suffix in {"SZ", "SZSE"} and symbol.startswith(("000", "001", "002", "003")):
        return "深市主板"
    return None


def risk_name(name: str) -> bool:
    normalized = name.replace(" ", "").upper()
    return (
        normalized.startswith(("ST", "*ST"))
        or normalized.endswith("退")
        or any(token in normalized for token in ("退市", "退市整理", "退市风险", "终止上市"))
    )


def load_calendar(path: Path) -> list[str]:
    dates: set[str] = set()
    for row in read_records(path):
        raw_open = first_value(row, "is_open", "open", "is_trading_day")
        if str(raw_open).strip().lower() not in {"1", "true", "yes", "open", "交易"}:
            continue
        dates.add(normalize_date(first_value(row, "cal_date", "trade_date", "date")))
    return sorted(dates)


def load_master(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in read_records(path):
        code = normalize_code(first_value(row, "ts_code", "code", "symbol"))
        if not code:
            continue
        copy = dict(row)
        copy["code"] = code
        copy["name"] = str(first_value(row, "name", "stock_name", "sec_name") or code)
        raw_list_date = first_value(row, "list_date", "listing_date", "listed")
        copy["listing_date"] = normalize_date(raw_list_date) if raw_list_date else None
        result[code] = copy
    return result


def load_st(path: Path | None) -> tuple[set[tuple[str, str]], dict[tuple[str, str], str]]:
    keys: set[tuple[str, str]] = set()
    names: dict[tuple[str, str], str] = {}
    if path is None:
        return keys, names
    for row in read_records(path):
        code = normalize_code(first_value(row, "ts_code", "code", "symbol"))
        trade_date = normalize_date(first_value(row, "trade_date", "date", "cal_date"))
        if code and trade_date:
            key = (code, trade_date)
            keys.add(key)
            names[key] = str(first_value(row, "name", "stock_name") or "")
    return keys, names


def load_price_map(path: Path, value_keys: tuple[str, ...], field_name: str) -> dict[tuple[str, str], float]:
    result: dict[tuple[str, str], float] = {}
    for row in read_records(path):
        code = normalize_code(first_value(row, "ts_code", "code", "symbol"))
        raw_date = first_value(row, "trade_date", "date", "cal_date")
        raw_value = first_value(row, *value_keys)
        if not code or raw_date in (None, "") or raw_value in (None, ""):
            continue
        key = (code, normalize_date(raw_date))
        value = numeric(raw_value, field_name)
        previous = result.get(key)
        if previous is not None and not math.isclose(previous, value, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"conflicting duplicate {field_name} for {key}: {previous} vs {value}")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute segmented 10-session gain winners from bulk daily data")
    parser.add_argument("--calendar", required=True, type=Path)
    parser.add_argument("--daily", required=True, type=Path)
    parser.add_argument("--adj-factor", type=Path, help="required unless --prices-adjusted")
    parser.add_argument("--stock-basic", required=True, type=Path)
    parser.add_argument("--st", type=Path, help="historical daily ST snapshots")
    parser.add_argument("--st-coverage", choices=("complete", "partial"), default="complete", help="declare whether --st covers every target date")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-count", type=int, default=30)
    parser.add_argument("--lookback", type=int, default=10)
    parser.add_argument("--threshold-days", type=int, default=60)
    parser.add_argument("--end-date", help="latest completed session, YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--prices-adjusted", action="store_true", help="daily close is already consistently adjusted")
    parser.add_argument("--allow-name-only-st", action="store_true", help="allow missing --st and only inspect current/display name")
    parser.add_argument("--markets", nargs="*", default=MARKETS)
    args = parser.parse_args()

    if args.target_count <= 0 or args.lookback <= 0 or args.threshold_days < 0:
        parser.error("target-count/lookback must be positive and threshold-days cannot be negative")
    if not args.prices_adjusted and args.adj_factor is None:
        parser.error("--adj-factor is required unless --prices-adjusted is set")
    if args.st is None and not args.allow_name_only_st:
        parser.error("--st is required for historical risk filtering; use --allow-name-only-st only as an explicit degraded mode")
    if args.st_coverage == "partial" and not args.allow_name_only_st:
        parser.error("partial ST coverage is degraded; add --allow-name-only-st to acknowledge the limitation")

    calendar = load_calendar(args.calendar)
    if args.end_date:
        end_date = normalize_date(args.end_date)
        calendar = [item for item in calendar if item <= end_date]
    required_sessions = args.target_count + args.lookback
    if len(calendar) < required_sessions:
        raise ValueError(f"calendar contains {len(calendar)} completed sessions; need at least {required_sessions}")
    selected_calendar = calendar[-required_sessions:]
    target_dates = selected_calendar[-args.target_count:]
    base_date_by_target = {
        selected_calendar[index]: selected_calendar[index - args.lookback]
        for index in range(args.lookback, len(selected_calendar))
    }

    master = load_master(args.stock_basic)
    closes = load_price_map(args.daily, ("close", "adj_close"), "close")
    factors = {} if args.prices_adjusted else load_price_map(args.adj_factor, ("adj_factor", "factor"), "adj_factor")
    st_keys, st_names = load_st(args.st)

    by_date_market: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    quality = defaultdict(int)
    target_set = set(target_dates)
    for (code, trade_date), close in closes.items():
        if trade_date not in target_set:
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
        info = master.get(code)
        if info is None:
            quality["missing_stock_master"] += 1
            continue
        market = market_for(code, info)
        if market not in args.markets:
            quality["outside_configured_markets"] += 1
            continue
        display_name = st_names.get((code, trade_date)) or str(info.get("name") or code)
        by_date_market[(trade_date, market)].append({
            "date": trade_date,
            "base_date": base_date,
            "market": market,
            "market_source": market,
            "code": code.split(".", 1)[0],
            "ts_code": code,
            "name": display_name,
            "pct": round(pct, 6),
            "close": close,
            "base_close": base_close,
            "listing_date": info.get("listing_date"),
            "url": None,
        })
        quality["return_candidates"] += 1

    winners: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for trade_date in target_dates:
        for market in args.markets:
            rows = sorted(by_date_market.get((trade_date, market), []), key=lambda item: (-item["pct"], item["ts_code"]))
            winner = None
            for candidate in rows:
                key = (candidate["ts_code"], trade_date)
                st = key in st_keys or risk_name(candidate["name"])
                reason = "历史ST/风险快照命中" if key in st_keys else ("名称带ST/退市风险标识" if risk_name(candidate["name"]) else "")
                listing_date = candidate.get("listing_date")
                new = False
                if not listing_date:
                    new = True
                    reason = reason or "上市日期无法核验"
                else:
                    age = (date.fromisoformat(trade_date) - date.fromisoformat(listing_date)).days
                    if age < args.threshold_days:
                        new = True
                        reason = reason or f"上市{age}个自然日，小于{args.threshold_days}日阈值"
                result = dict(candidate, st=st, new=new, qualified=not st and not new)
                if not result["qualified"]:
                    result["exclusion_reason"] = reason
                    audit.append(result)
                    continue
                winner = result
                break
            if winner:
                winners.append(winner)
            else:
                missing.append({"date": trade_date, "market": market, "reason": "无具备完整价格、复权、主数据和资格信息的候选"})

    source_mode = "adjusted_close_input" if args.prices_adjusted else "raw_close_times_adj_factor"
    warnings: list[str] = []
    if args.st is None:
        warnings.append("未提供历史ST快照，仅按股票名称过滤；历史风险状态可能漏判。")
    elif args.st_coverage == "partial":
        warnings.append("ST快照只覆盖部分目标日期；未覆盖日期仅按股票主数据名称过滤，历史风险状态可能漏判。")
    if missing:
        warnings.append(f"存在 {len(missing)} 个日期×市场缺失组合，不得填0。")
    output = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dates": target_dates,
        "markets": args.markets,
        "selection_mode": "by_market",
        "lookback_trading_sessions": args.lookback,
        "return_formula": "adjusted_close(t) / adjusted_close(t-lookback) - 1",
        "adjustment_mode": source_mode,
        "new_stock_threshold_days": args.threshold_days,
        "trend_ui": {
            "default_markets": [market for market in DEFAULT_TREND_MARKETS if market in args.markets],
            "available_markets": args.markets,
            "gain_bands": GAIN_BANDS,
            "risk_notice": "涨幅区间用于表达短期情绪强度，不代表确定性收益；情绪越强，波动与尾部风险通常越大。",
        },
        "winners": winners,
        "audit_excluded_count": len(audit),
        "audit_excluded": audit,
        "missing_combinations": missing,
        "quality": dict(sorted(quality.items())),
        "source": {
            "calendar": str(args.calendar),
            "daily": str(args.daily),
            "adj_factor": str(args.adj_factor) if args.adj_factor else None,
            "stock_basic": str(args.stock_basic),
            "st": str(args.st) if args.st else None,
            "st_coverage": args.st_coverage if args.st else "none",
        },
        "warning": "；".join(warnings) if warnings else "结果由完整输入横截面本地计算；仍需核验数据供应商覆盖范围和收盘时点。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "output": str(args.output),
        "calendar_sessions": len(selected_calendar),
        "target_dates": len(target_dates),
        "winners": len(winners),
        "expected_combinations": len(target_dates) * len(args.markets),
        "missing_combinations": len(missing),
        "audit_excluded": len(audit),
        "quality": dict(sorted(quality.items())),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not missing else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
