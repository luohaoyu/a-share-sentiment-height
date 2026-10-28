#!/usr/bin/env python3
"""Build a deterministic SVG/HTML report from filtered market trend JSON."""
from __future__ import annotations

import argparse
import html
import json
from itertools import combinations
from pathlib import Path

MARKET_COLORS = {
    "沪市主板": "#c2410c",
    "深市主板": "#2563eb",
    "创业板": "#7c3aed",
    "科创板": "#0891b2",
    "北交所": "#059669",
}
DEFAULT_MARKETS = ["沪市主板", "深市主板"]
GAIN_BANDS = [
    {"key": "0-30", "label": "0%—30%", "min": 0, "max": 30, "color": "#E6F4EA", "stroke": "#188038"},
    {"key": "30-50", "label": "30%—50%", "min": 30, "max": 50, "color": "#E8F0FE", "stroke": "#1967D2"},
    {"key": "50-100", "label": "50%—100%", "min": 50, "max": 100, "color": "#FEF7E0", "stroke": "#B06000"},
    {"key": "100-200", "label": "100%—200%", "min": 100, "max": 200, "color": "#FCE8E6", "stroke": "#C5221F"},
    {"key": "200-300", "label": "200%—300%", "min": 200, "max": 300, "color": "#FAD2CF", "stroke": "#A50E0E"},
    {"key": "300-plus", "label": "300%+", "min": 300, "max": None, "color": "#F4CCCC", "stroke": "#85200C"},
]


def js_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def make_svg(data: dict, selected_markets: list[str] | None = None) -> str:
    dates = data.get("dates", [])
    all_markets = data.get("markets", [])
    configured = data.get("trend_ui", {})
    markets = selected_markets or configured.get("default_markets") or [m for m in DEFAULT_MARKETS if m in all_markets] or all_markets
    winners = {(row.get("date"), row.get("market")): row for row in data.get("winners", [])}
    width, height = 1240, 580
    left, right, top, bottom = 76, 1190, 55, 455
    values = [float(row.get("pct", 0)) for row in data.get("winners", []) if row.get("market") in markets]
    vmax = max(values or [100])
    y_max = 1250 if vmax > 1000 else 600 if vmax > 500 else 250 if vmax > 200 else 100
    step = (right - left) / max(1, len(dates) - 1)

    def x_at(index: int) -> float:
        return left + index * step

    def y_at(value: float) -> float:
        return bottom - value / y_max * (bottom - top)

    grid = []
    for value in (0, y_max * 0.25, y_max * 0.5, y_max * 0.75, y_max):
        y = y_at(value)
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#e5e7eb"/>'
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" fill="#667085" font-size="11">{value:.0f}%</text>'
        )

    band_rects = []
    for band in GAIN_BANDS:
        band_min = band["min"]
        band_max = band["max"] if band["max"] is not None else y_max
        if band_min >= y_max:
            continue
        clipped_max = min(band_max, y_max)
        y_top, y_bottom = y_at(clipped_max), y_at(band_min)
        band_rects.append(
            f'<rect x="{left}" y="{y_top:.1f}" width="{right - left}" height="{max(0, y_bottom - y_top):.1f}" '
            f'fill="{band["color"]}" opacity="0.42"><title>{html.escape(band["label"])}：情绪强度区间，风险与波动同步抬升</title></rect>'
        )

    lines = []
    for market in markets:
        points = [
            (x_at(i), y_at(float(winners[(day, market)]["pct"])), winners[(day, market)])
            for i, day in enumerate(dates)
            if (day, market) in winners
        ]
        if not points:
            continue
        color = MARKET_COLORS.get(market, "#475467")
        polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
        dots = "".join(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" stroke="#fff" stroke-width="1.5">'
            f'<title>{html.escape(row["date"])} {html.escape(row["name"])} {html.escape(row["code"])} +{float(row["pct"]):.2f}%</title></circle>'
            for x, y, row in points
        )
        lines.append(f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>{dots}')

    labels = "".join(
        f'<text x="{x_at(i):.1f}" y="485" text-anchor="middle" fill="#667085" font-size="11">{html.escape(day[5:])}</text>'
        for i, day in enumerate(dates)
        if i % 3 == 0 or i == len(dates) - 1
    )
    legend = "".join(
        f'<g><line x1="{780 + i * 115}" y1="24" x2="{805 + i * 115}" y2="24" stroke="{MARKET_COLORS.get(m, "#475467")}" stroke-width="3"/>'
        f'<text x="{812 + i * 115}" y="28" fill="#475467" font-size="12">{html.escape(m)}</text></g>'
        for i, m in enumerate(markets)
    )
    band_legend = "".join(
        f'<g><rect x="{76 + i * 160}" y="505" width="12" height="12" rx="2" fill="{band["color"]}" stroke="{band["stroke"]}" stroke-width="0.8"/>'
        f'<text x="{94 + i * 160}" y="515" fill="#475467" font-size="11">{html.escape(band["label"])}</text></g>'
        for i, band in enumerate(GAIN_BANDS)
    )
    mobile_band_legend = "".join(
        f'<g><rect x="{58 + (i % 3) * 205}" y="{500 + (i // 3) * 25}" width="12" height="12" rx="2" fill="{band["color"]}" stroke="{band["stroke"]}" stroke-width="0.8"/>'
        f'<text x="{76 + (i % 3) * 205}" y="{510 + (i // 3) * 25}" fill="#475467" font-size="11">{html.escape(band["label"])}</text></g>'
        for i, band in enumerate(GAIN_BANDS)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="分市场每日10日区间涨幅冠军趋势图，背景按涨幅区间表达情绪强度" style="width:100%;height:auto;display:block"><style>.mobile-legend{{display:none}}@media(max-width:640px){{.desktop-legend{{display:none}}.mobile-legend{{display:block}}}}</style>'
        f'{legend}{"".join(band_rects)}{"".join(grid)}{"".join(lines)}{labels}<g class="desktop-legend">{band_legend}</g><g class="mobile-legend">{mobile_band_legend}</g>'
        '<text x="76" y="567" fill="#667085" font-size="11">风险提示：情绪越强，短期波动与尾部风险越大；区间不代表确定性收益。</text></svg>'
    )


def render_report(data: dict) -> str:
    dates = data.get("dates", [])
    markets = data.get("markets", [])
    winners = data.get("winners", [])
    by_key = {(row.get("date"), row.get("market")): row for row in winners}
    ui = data.get("trend_ui", {})
    default_markets = ui.get("default_markets") or [m for m in DEFAULT_MARKETS if m in markets] or markets
    available_markets = ui.get("available_markets") or markets
    risk_notice = ui.get("risk_notice", "涨幅区间用于表达短期情绪强度，不代表确定性收益；情绪越强，波动与尾部风险通常越大。")

    rows = []
    for day in dates:
        for market in markets:
            row = by_key.get((day, market))
            if row:
                rows.append(
                    f'<tr data-market="{html.escape(market)}"><td>{html.escape(day)}</td><td>{html.escape(market)}</td>'
                    f'<td>{html.escape(str(row.get("name", "")))}</td><td>{html.escape(str(row.get("code", "")))}</td>'
                    f'<td class="pct">+{float(row.get("pct", 0)):.2f}%</td><td>{html.escape(str(row.get("listing_date", "")))}</td></tr>'
                )
            else:
                rows.append(
                    f'<tr class="missing" data-market="{html.escape(market)}"><td>{html.escape(day)}</td><td>{html.escape(market)}</td>'
                    '<td colspan="4">无可核验记录或来源展示范围不足</td></tr>'
                )

    excluded = data.get("audit_excluded", data.get("audit_excluded_sample", []))
    excluded_rows = "".join(
        f'<tr><td>{html.escape(str(row.get("date", "")))}</td><td>{html.escape(str(row.get("market", "")))}</td>'
        f'<td>{html.escape(str(row.get("name", "")))}</td><td>{html.escape(str(row.get("code", "")))}</td>'
        f'<td>+{float(row.get("pct", 0)):.2f}%</td><td>{html.escape(str(row.get("exclusion_reason", row.get("new_reason", ""))))}</td></tr>'
        for row in excluded[:120]
    )
    market_controls = "".join(
        f'<label><input type="checkbox" class="market-toggle" value="{html.escape(market)}" {"checked" if market in default_markets else ""}>{html.escape(market)}</label>'
        for market in available_markets
    )
    band_legend = "".join(
        f'<span class="band"><i style="background:{band["color"]};border-color:{band["stroke"]}"></i>{html.escape(band["label"])}</span>'
        for band in GAIN_BANDS
    )
    chart_data = {"dates": dates, "markets": markets, "winners": winners, "trend_ui": ui}
    chart_variants = {"|".join(combo): make_svg(data, list(combo)) for size in range(1, len(available_markets) + 1) for combo in combinations(available_markets, size)}
    title_date = f'{dates[0]} 至 {dates[-1]}' if dates else "未提供日期"
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>分市场10日区间涨幅冠军</title><style>
*{{box-sizing:border-box}}html,body{{margin:0;padding:0;max-width:100%;overflow-x:hidden}}body{{background:#f5f7fb;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif}}.wrap{{width:100%;max-width:1320px;margin:auto;padding:28px 22px 50px}}.panel{{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:18px;margin-top:18px;box-shadow:0 2px 8px #1018280b;overflow:hidden}}h1{{margin:8px 0;font-size:28px}}h2{{margin:0 0 12px;font-size:18px}}h3{{font-size:14px;margin:16px 0 8px}}.muted{{color:#667085;font-size:13px;line-height:1.7}}.controls{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:12px 0}}.controls label{{font-size:13px;cursor:pointer;padding:7px 9px;border:1px solid #d0d5dd;border-radius:8px;background:#fff}}.controls input{{accent-color:#2563eb;margin-right:5px}}.bands{{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 2px}}.band{{font-size:12px;color:#475467;display:inline-flex;align-items:center;gap:5px}}.band i{{display:inline-block;width:12px;height:12px;border:1px solid;border-radius:2px}}.risk{{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;padding:9px 12px;border-radius:8px;font-size:13px;line-height:1.6}}#trend-chart{{width:100%;max-width:100%;overflow:hidden}}#trend-chart svg{{display:block;width:100%;max-width:100%;height:auto;min-height:260px}}.table-wrap{{width:100%;overflow-x:auto;overflow-y:auto;max-height:760px;-webkit-overflow-scrolling:touch}}table{{width:100%;border-collapse:collapse;font-size:12px;min-width:860px}}th,td{{padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:left;white-space:nowrap}}th{{position:sticky;top:0;background:#f8fafc;color:#475467}}.pct{{color:#d92d20;font-weight:700;text-align:right}}.missing{{color:#98a2b3;background:#fafafa}}@media (max-width:640px){{.wrap{{padding:14px 10px 28px}}.panel{{padding:12px;border-radius:10px;margin-top:12px}}h1{{font-size:22px;line-height:1.3}}h2{{font-size:16px}}.muted{{font-size:12px}}.controls{{gap:6px}}.controls label{{font-size:12px;padding:6px 7px}}.bands{{gap:7px 10px}}.risk{{font-size:12px;padding:8px 10px}}#trend-chart svg{{min-height:220px}}.table-wrap{{margin:0 -2px;width:calc(100% + 4px)}}table{{font-size:11px;min-width:720px}}th,td{{padding:8px 7px}}}}
</style></head><body><div class="wrap"><div class="muted">分市场历史横截面统计 · 数据完全来自输入 JSON</div><h1>每日 10 日区间涨幅冠军</h1><p class="muted">交易日：{html.escape(title_date)}；趋势分析区默认展示：{html.escape("、".join(default_markets))}；其他市场可切换查看；新股阈值：{html.escape(str(data.get("new_stock_threshold_days", "未指定")))} 个自然日。</p><section class="panel"><h2>趋势分析区</h2><div class="controls"><strong>市场：</strong>{market_controls}</div><div class="bands"><strong>涨幅情绪区间：</strong>{band_legend}</div><div class="risk">{html.escape(risk_notice)}</div><div id="trend-chart">{make_svg(data, default_markets)}</div></section><section class="panel"><h2>每日 × 市场明细</h2><div class="table-wrap"><table><thead><tr><th>交易日</th><th>市场</th><th>冠军</th><th>代码</th><th>10日涨幅</th><th>上市日期</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section><section class="panel"><h2>过滤审计</h2><div class="table-wrap"><table><thead><tr><th>日期</th><th>市场</th><th>股票</th><th>代码</th><th>涨幅</th><th>排除原因</th></tr></thead><tbody>{excluded_rows or '<tr><td colspan="6">无审计记录</td></tr>'}</tbody></table></div></section><section class="panel"><h2>数据限制</h2><p class="muted">{html.escape(str(data.get("warning", "未提供额外限制说明。")))}</p></section></div><script>
const TREND_DATA = {js_json(chart_data)};
const DEFAULT_MARKETS = {js_json(default_markets)};
const AVAILABLE_MARKETS = {js_json(available_markets)};
const CHARTS = {js_json(chart_variants)};
const toggles = Array.from(document.querySelectorAll('.market-toggle'));
const tableRows = Array.from(document.querySelectorAll('tbody tr[data-market]'));
function refreshMarkets() {{
  const selected = toggles.filter((item) => item.checked).map((item) => item.value);
  tableRows.forEach((row) => {{ row.hidden = !selected.includes(row.dataset.market); }});
  const key = AVAILABLE_MARKETS.filter((market) => selected.includes(market)).join('|');
  document.getElementById('trend-chart').innerHTML = CHARTS[key] || '<p class="muted">请至少选择一个市场。</p>';
}}
toggles.forEach((item) => item.addEventListener('change', refreshMarkets));
</script></body></html>'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Render static market trend HTML")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    report = render_report(data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(json.dumps({"output": str(args.output), "rows": len(data.get("dates", [])) * len(data.get("markets", [])), "svg": True, "default_markets": data.get("trend_ui", {}).get("default_markets", DEFAULT_MARKETS), "gain_bands": [band["label"] for band in GAIN_BANDS]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
