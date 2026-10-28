#!/usr/bin/env python3
"""Apply deterministic market, ST, new-stock, and ranking rules."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

MARKET_PREFIXES = {
    "沪市主板": ("600", "601", "603", "605"),
    "深市主板": ("000", "001", "002", "003"),
    "创业板": ("300",),
    "科创板": ("688",),
    "北交所": ("920",),
}
DEFAULT_TREND_MARKETS = ["沪市主板", "深市主板"]
GAIN_BANDS = [
    {"key": "0-30", "label": "0%—30%", "min": 0, "max": 30, "color": "#E6F4EA", "stroke": "#188038"},
    {"key": "30-50", "label": "30%—50%", "min": 30, "max": 50, "color": "#E8F0FE", "stroke": "#1967D2"},
    {"key": "50-100", "label": "50%—100%", "min": 50, "max": 100, "color": "#FEF7E0", "stroke": "#B06000"},
    {"key": "100-200", "label": "100%—200%", "min": 100, "max": 200, "color": "#FCE8E6", "stroke": "#C5221F"},
    {"key": "200-300", "label": "200%—300%", "min": 200, "max": 300, "color": "#FAD2CF", "stroke": "#A50E0E"},
    {"key": "300-plus", "label": "300%+", "min": 300, "max": None, "color": "#F4CCCC", "stroke": "#85200C"},
]


def classify_market(code: str) -> str | None:
    code = code.replace(".", "")[:6]
    for market, prefixes in MARKET_PREFIXES.items():
        if code.startswith(prefixes):
            return market
    return None


def is_st_or_risk(name: str, risk_flag: object = False) -> tuple[bool, str]:
    normalized = name.replace(" ", "").upper()
    if bool(risk_flag):
        return True, "数据源风险标识"
    if normalized.startswith(("*ST", "ST")):
        return True, "名称带ST风险标识"
    if any(token in normalized for token in ("退市整理", "退市风险", "终止上市")):
        return True, "名称带退市/风险标识"
    return False, ""


def is_new_stock(row: dict, trade_date: str, threshold_days: int) -> tuple[bool, str]:
    name = row.get("name", "").replace(" ", "").upper()
    if name.startswith(("N", "C")):
        return True, "名称带N/C新股标识"
    listing_date = row.get("listing_date")
    if not listing_date:
        return True, "上市日期无法核验"
    try:
        age = (date.fromisoformat(trade_date) - date.fromisoformat(listing_date)).days
    except ValueError:
        return True, "上市日期格式无效"
    if age < threshold_days:
        return True, f"上市{age}个自然日，小于{threshold_days}日阈值"
    return False, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter and select market winners")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--threshold-days", type=int, default=60)
    parser.add_argument("--markets", nargs="*", default=list(MARKET_PREFIXES))
    parser.add_argument("--overall", action="store_true", help="select one qualified winner per date across the configured markets")
    args = parser.parse_args()

    source = json.loads(args.input.read_text(encoding="utf-8"))
    # Accept both the normalized candidate schema and the previously generated
    # market_trend_data.json schema so the skill can reprocess existing runs.
    if source.get("rows"):
        candidates = source["rows"]
    elif source.get("winners"):
        candidates = [dict(row, listing_date=row.get("listing", row.get("listing_date", row.get("listed")))) for row in source["winners"]]
        # Existing winner records are already qualified; preserve their status
        # instead of treating absent risk metadata as a reason to re-filter.
        for row in candidates:
            row.setdefault("listing_date_verified", bool(row.get("listing_date")))
            row.setdefault("risk_flag", False)
    else:
        candidates = []
    # --overall selects one winner per date after filtering, while the default
    # mode selects one winner per date and market.
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in candidates:
        market = classify_market(row.get("code", ""))
        if market not in args.markets:
            continue
        key = (row.get("date", ""), "全市场") if args.overall else (row.get("date", ""), market)
        enriched = dict(row)
        enriched["market_source"] = market
        grouped.setdefault(key, []).append(enriched)

    winners: list[dict] = []
    audit: list[dict] = []
    for (trade_date, market), rows in sorted(grouped.items()):
        winner = None
        for candidate in sorted(rows, key=lambda item: (-float(item.get("pct", 0)), item.get("code", ""))):
            st, st_reason = is_st_or_risk(candidate.get("name", ""), candidate.get("risk_flag", False))
            if source.get("winners") and candidate.get("new") is False and candidate.get("st") is False:
                new, new_reason = False, ""
            else:
                new, new_reason = is_new_stock(candidate, trade_date, args.threshold_days)
            result = dict(candidate)
            result.update({
                "market": market,
                "st": st,
                "new": new,
                "new_reason": new_reason,
                "qualified": not st and not new,
            })
            if st or new:
                result["exclusion_reason"] = st_reason or new_reason
                audit.append(result)
                continue
            winner = result
            break
        if winner:
            winners.append(winner)

    input_dates = source.get("dates") or sorted({row.get("date") for row in candidates if row.get("date")})
    output_markets = ["全市场"] if args.overall else args.markets
    output = {
        "dates": input_dates,
        "markets": output_markets,
        "selection_mode": "overall" if args.overall else "by_market",
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
        "input_manifest": source.get("manifest", []),
        "source": source.get("source", {}),
        "warning": "结果仅覆盖输入候选集；若输入页面存在展示阈值或不完整横截面，不得将缺失组合解释为涨幅为0。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    expected = len(output["dates"]) * len(output_markets)
    print(json.dumps({"output": str(args.output), "dates": len(output["dates"]), "winners": len(winners), "expected_combinations": expected, "excluded": len(audit), "selection_mode": output["selection_mode"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
