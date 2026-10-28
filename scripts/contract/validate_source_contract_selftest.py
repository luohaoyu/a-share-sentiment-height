#!/usr/bin/env python3
"""validate_source_contract.py 的离线自测：52 条规则逐条验活。

做法：
1. 造一份完全合规的契约数据集（fixture），断言 0 error / 0 warning。
2. 对每条规则施加一个针对性破坏，断言该规则确实报出来。

只有全部规则都能被"弄脏一次并抓到"，才算校验器真的覆盖了文档。
不需要 akshare，不发网络请求。

    python validate_source_contract_selftest.py
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_source_contract as V  # noqa: E402

DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
        "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
AS_OF = "2026-10-23"
NOW = datetime(2026, 10, 25, 22, 0, 0)

STOCKS = [
    ("600519.SH", "贵州茅台", "沪市主板", "SH", "2001-08-27"),
    ("000001.SZ", "平安银行", "深市主板", "SZ", "1991-04-03"),
    ("300750.SZ", "宁德时代", "创业板", "SZ", "2018-06-11"),
    ("688111.SH", "金山办公", "科创板", "SH", "2019-11-18"),
    ("600001.SH", "示例科技", "沪市主板", "SH", "2000-01-01"),
]
BASE = {"600519.SH": 1000.0, "300750.SZ": 200.0}
SERIES = {"hfq": "sina:hfq:listing", "qfq": "sina:qfq:latest", "none": "sina:none:raw"}


def price(ts_code: str, idx: int) -> float:
    return round(BASE[ts_code] * (1 + 0.01 * idx), 4)


def build() -> dict:
    calendar = [{"cal_date": d, "is_open": 1} for d in DAYS]
    security = [
        {"ts_code": ts, "symbol": ts[:6], "name": name, "exchange": ex,
         "market": market, "list_date": ld, "list_status": "L",
         "market_group": "沪深主板" if market in ("沪市主板", "深市主板") else "科创成长板"}
        for ts, name, market, ex, ld in STOCKS
    ]

    daily = []
    for ts in BASE:
        for idx, d in enumerate(DAYS):
            c = price(ts, idx)
            for adj in ("hfq", "qfq", "none"):
                scale = 10.0 if adj == "hfq" else 1.0
                daily.append({
                    "trade_date": d, "ts_code": ts, "price_adjust": adj,
                    "open": round(c * scale * 0.99, 4),
                    "high": round(c * scale * 1.02, 4),
                    "low": round(c * scale * 0.98, 4),
                    "close": round(c * scale, 4),
                    "pre_close": None, "pct_chg": None,
                    "volume": 1_000_000.0, "amount": 1.0e8,
                    "turnover_rate": 0.46, "float_share": 1.0e9,
                    "price_basis": "real", "series_id": SERIES[adj], "source": "sina",
                })

    limit_close = price("300750.SZ", DAYS.index("2026-10-20"))
    limit_up = [{
        "trade_date": "2026-10-20", "ts_code": "300750.SZ", "rank": 1,
        "close": limit_close, "pct_chg": 10.02, "turnover_rate": 2.62,
        "sealed_amount": 3.2e8, "first_seal_time": "09:25:00", "last_seal_time": "09:25:00",
        "open_times": 0, "consecutive_boards": 1, "limit_stat": "1/1",
        "limit_price": None, "pool_status": "ok", "source": "eastmoney",
        "fetched_at": "2026-10-20T16:10:00+08:00",
    }]
    limit_up_days = [
        {"trade_date": "2026-10-20", "pool_status": "ok", "count": 1},
        {"trade_date": "2026-10-21", "pool_status": "ok", "count": 0},
    ]

    risk = []
    for d in DAYS:
        for ts, name, *_ in STOCKS:
            risky = ts == "600001.SH"
            risk.append({
                "trade_date": d, "ts_code": ts,
                "risk_flag": 1 if risky else 0,
                "risk_type": "*ST" if risky else None,
                "name_snapshot": ("*ST" + name) if risky else name,
                "coverage_status": "ok" if d == AS_OF else "partial",
                "source": "name_snapshot",
                "fetched_at": "2026-10-23T16:10:00+08:00",
            })

    capability = [
        {"source_id": "akshare_sina_calendar", "object_name": "calendar", "supported": 1,
         "field_support": {"cal_date": "supported", "is_open": "absent"},
         "field_fallback": {"is_open": {"mode": "constant", "value": 1,
                                        "degrade": "上游无 is_open 列"}},
         "window_start": None, "window_end": AS_OF, "history_backfill": 1,
         "update_latency": None, "drift_policy": "stable",
         "request_model": "bulk", "rate_limit_qps": None,
         "probed_at": "2026-10-20T10:00:00+08:00"},
        {"source_id": "akshare_sh_info", "object_name": "security", "supported": 1,
         "field_support": {"ts_code": "supported", "symbol": "supported",
                           "name": "supported", "list_date": "supported",
                           "list_status": "absent"},
         "field_fallback": {"list_status": {"mode": "constant", "value": "L",
                                            "degrade": "名录不返回上市状态"}},
         "window_start": None, "window_end": AS_OF, "history_backfill": 1,
         "update_latency": "PT0S", "drift_policy": "stable",
         "request_model": "bulk", "rate_limit_qps": None,
         "probed_at": "2026-10-20T10:00:00+08:00"},
        {"source_id": "akshare_sina_daily", "object_name": "daily_kline", "supported": 1,
         "field_support": {"open": "supported", "high": "supported", "low": "supported",
                           "close": "supported", "pre_close": "absent", "pct_chg": "derived",
                           "volume": "supported", "amount": "supported"},
         "window_start": None, "window_end": None, "history_backfill": 1,
         "update_latency": None, "drift_policy": "stable",
         "request_model": "per_symbol", "rate_limit_qps": 8.8,
         "probed_at": "2026-10-20T10:00:00+08:00"},
        {"source_id": "akshare_em_ztpool", "object_name": "limit_up_history", "supported": 1,
         "field_support": {"close": "supported", "limit_price": "absent",
                           "consecutive_boards": "supported"},
         "window_start": "2026-10-01", "window_end": AS_OF, "history_backfill": 1,
         "update_latency": "PT30M", "drift_policy": "revised",
         "request_model": "per_date", "rate_limit_qps": 8.8,
         "probed_at": "2026-10-20T10:00:00+08:00"},
        {"source_id": "akshare_em_spot", "object_name": "risk_status", "supported": 1,
         "field_support": {"name_snapshot": "supported", "risk_flag": "partial"},
         "window_start": DAYS[0], "window_end": AS_OF, "history_backfill": 0,
         "update_latency": "PT0S", "drift_policy": "stable",
         "request_model": "bulk", "rate_limit_qps": None,
         "probed_at": "2026-10-20T10:00:00+08:00"},
    ]

    return {"calendar": calendar, "security": security, "daily": daily,
            "limit_up": limit_up, "limit_up_days": limit_up_days, "risk": risk,
            "capability": capability, "manifest": {"as_of": AS_OF, "sessions": DAYS}}


FILES = {
    "calendar": "calendar.json", "security": "security.json",
    "daily": "daily_kline.json", "limit_up": "limit_up_history.json",
    "limit_up_days": "limit_up_days.json", "risk": "risk_status.json",
    "capability": "source_capability.json", "manifest": "manifest.json",
}


def write_fixture(root: Path, fx: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for attr, name in FILES.items():
        (root / name).write_text(
            json.dumps(fx[attr], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return root


def to_dataset(fx: dict) -> V.Dataset:
    """内存构造，保留 date 对象等 Python 原生类型（规则 30/31/35 需要）。"""
    ds = V.Dataset()
    ds.calendar = copy.deepcopy(fx["calendar"])
    ds.security = copy.deepcopy(fx["security"])
    ds.daily = copy.deepcopy(fx["daily"])
    ds.limit_up = copy.deepcopy(fx["limit_up"])
    ds.limit_up_days = copy.deepcopy(fx["limit_up_days"])
    ds.risk = copy.deepcopy(fx["risk"])
    ds.capability = copy.deepcopy(fx["capability"])
    ds.manifest = copy.deepcopy(fx["manifest"])
    ds.present = set(FILES) - {"manifest"}
    return ds


def run(fx: dict, baseline: dict | None = None) -> tuple[set[int], list, list]:
    ds = to_dataset(fx)
    bs = to_dataset(baseline) if baseline else None
    violations, skipped = V.validate(ds, baseline=bs, now=NOW)
    return {x.rule for x in violations}, violations, skipped


def run_via_files(fx: dict) -> tuple[set[int], list]:
    """走一遍磁盘装载，验证 load_dataset 本身可用。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = write_fixture(Path(tmp) / "cur", fx)
        ds = V.load_dataset(root)
        violations, _ = V.validate(ds, now=NOW)
    return {x.rule for x in violations}, violations


def find(rows: list[dict], **cond) -> dict:
    for r in rows:
        if all(r.get(k) == v for k, v in cond.items()):
            return r
    raise KeyError(cond)


# --------------------------------------------------------------------------- #
# 逐条破坏
# --------------------------------------------------------------------------- #
def cases() -> list[tuple[int, str, "callable", str | None]]:
    """返回 (规则号, 说明, 破坏函数, 模式)。模式 baseline 表示需要基线比对。"""

    def m(fn):
        return lambda fx: fn(fx)

    return [
        (1, "日历重复日期", m(lambda fx: fx["calendar"].append({"cal_date": DAYS[-1], "is_open": 1})), None),
        (2, "is_open=2", m(lambda fx: fx["calendar"][0].__setitem__("is_open", 2)), None),
        (3, "ts_code 重复", m(lambda fx: fx["security"].append(dict(fx["security"][0]))), None),
        (4, "symbol 位数错", m(lambda fx: fx["security"][0].__setitem__("symbol", "60051")), None),
        (5, "market 非闭集", m(lambda fx: fx["security"][0].__setitem__("market", "主板")), None),
        (6, "exchange 与 market 冲突", m(lambda fx: fx["security"][0].__setitem__("exchange", "SZ")), None),
        (7, "list_date 晚于日K最早日", m(lambda fx: fx["security"][0].__setitem__("list_date", "2026-12-31")), None),
        (8, "list_status=X", m(lambda fx: fx["security"][0].__setitem__("list_status", "X")), None),
        (9, "日K主键重复", m(lambda fx: fx["daily"].append(dict(fx["daily"][0]))), None),
        (10, "price_adjust 非法值", m(lambda fx: fx["daily"][0].__setitem__("price_adjust", "adj")), None),
        (11, "四价半组", m(lambda fx: fx["daily"][0].__setitem__("high", None)), None),
        (12, "收盘价为负", m(lambda fx: fx["daily"][0].__setitem__("close", -1.0)), None),
        (13, "成交量为负", m(lambda fx: fx["daily"][0].__setitem__("volume", -5.0)), None),
        (14, "日K落在非开市日", m(lambda fx: fx["daily"][0].__setitem__("trade_date", "2026-10-17")), None),
        (15, "出现合成价", m(lambda fx: fx["daily"][0].__setitem__("price_basis", "synthetic")), None),
        (16, "涨停主键重复", m(lambda fx: fx["limit_up"].append(dict(fx["limit_up"][0]))), None),
        (17, "pool_status 非法", m(lambda fx: fx["limit_up"][0].__setitem__("pool_status", "maybe")), None),
        (18, "涨停落在非开市日", m(lambda fx: fx["limit_up"][0].__setitem__("trade_date", "2026-10-17")), None),
        (19, "涨停股票不在名录", m(lambda fx: fx["limit_up"][0].__setitem__("ts_code", "999999.SH")), None),
        (20, "同日 pool_status 不一致", m(lambda fx: fx["limit_up"].append(
            {**fx["limit_up"][0], "ts_code": "600519.SH", "pool_status": "error",
             "close": price("600519.SH", 6)})), None),
        (21, "连板数为 0", m(lambda fx: fx["limit_up"][0].__setitem__("consecutive_boards", 0)), None),
        (22, "首封晚于末封", m(lambda fx: fx["limit_up"][0].__setitem__("first_seal_time", "10:00:00")), None),
        (23, "count 与实际记录不符", m(lambda fx: fx["limit_up_days"][0].__setitem__("count", 5)), None),
        (24, "涨停收盘价与日K不符", m(lambda fx: fx["limit_up"][0].__setitem__("close", 999.0)), None),
        (25, "历史日记录被改写", m(lambda fx: fx["limit_up"][0].__setitem__("fetched_at", "2026-10-25T09:00:00+08:00")), "baseline"),
        (26, "超窗口标 ok 且为空", m(lambda fx: fx["limit_up_days"].append(
            {"trade_date": "2026-09-30", "pool_status": "ok", "count": 0})), None),
        (27, "limit_price 非空", m(lambda fx: fx["limit_up"][0].__setitem__("limit_price", 12.3)), None),
        (28, "封板时间残留原串", m(lambda fx: fx["limit_up"][0].__setitem__("first_seal_time", "092500")), None),
        (29, "日历含未来日期", m(lambda fx: fx["calendar"].append({"cal_date": "2026-10-26", "is_open": 1})), None),
        (30, "日历残留 date 对象", m(lambda fx: fx["calendar"][0].__setitem__("cal_date", date(2026, 10, 12))), None),
        (31, "list_date 为 date 对象", m(lambda fx: fx["security"][0].__setitem__("list_date", date(2001, 8, 27))), None),
        (32, "简称含连续空格", m(lambda fx: fx["security"][0].__setitem__("name", "茅  台")), None),
        (33, "口径未打标", m(lambda fx: fx["daily"][0].__setitem__("price_adjust", None)), None),
        (34, "换手率漏除 100", m(lambda fx: fx["daily"][0].__setitem__("turnover_rate", 460.0)), None),
        (35, "trade_date 为 date 对象", m(lambda fx: fx["daily"][0].__setitem__("trade_date", date(2026, 10, 12))), None),
        (36, "series_id 格式错", m(lambda fx: fx["daily"][0].__setitem__("series_id", "bad-format")), None),
        (37, "同口径跨源拼接", m(lambda fx: fx["daily"][0].__setitem__("series_id", "tushare:hfq:listing")), None),
        (38, "series 源与 source 不符", m(lambda fx: fx["daily"][0].__setitem__("source", "tushare")), None),
        (39, "风险主键重复", m(lambda fx: fx["risk"].append(dict(fx["risk"][0]))), None),
        (40, "risk_flag=2", m(lambda fx: fx["risk"][0].__setitem__("risk_flag", 2)), None),
        (41, "flag=1 但无类型", m(lambda fx: find(fx["risk"], ts_code="600001.SH").__setitem__("risk_type", None)), None),
        (42, "partial 下把未覆盖记为 0", m(lambda fx: find(fx["risk"], trade_date=DAYS[0], ts_code="600519.SH").update(
            {"risk_flag": 0, "name_snapshot": None})), None),
        (43, "名称快照回填历史却标 ok", m(lambda fx: find(fx["risk"], trade_date=DAYS[0], ts_code="600519.SH").__setitem__(
            "coverage_status", "ok")), None),
        (44, "风险落在非开市日", m(lambda fx: fx["risk"][0].__setitem__("trade_date", "2026-10-17")), None),
        (45, "能力声明主键重复", m(lambda fx: fx["capability"].append(dict(fx["capability"][0]))), None),
        (46, "absent 字段却有值", m(lambda fx: fx["daily"][0].__setitem__("pre_close", 100.0)), None),
        (47, "supported=0 却有记录", m(lambda fx: find(fx["capability"], object_name="limit_up_history").__setitem__(
            "supported", 0)), None),
        (48, "数据早于 window_start", m(lambda fx: find(fx["capability"], object_name="limit_up_history").__setitem__(
            "window_start", "2026-10-21")), None),
        (49, "声明 revised 却改写历史", m(lambda fx: fx["limit_up"][0].__setitem__("fetched_at", "2026-10-25T09:00:00+08:00")), "baseline"),
        (50, "能力声明过期", m(lambda fx: fx["capability"][0].__setitem__("probed_at", "2025-01-01T00:00:00+08:00")), None),
        (51, "derived 字段未标 derived", m(lambda fx: fx["daily"][0].__setitem__("pct_chg", 1.5)), None),
        (52, "兜底常量与实际不符", m(lambda fx: find(fx["capability"], object_name="calendar")["field_fallback"][
            "is_open"].__setitem__("value", 0)), None),
    ]


def main() -> int:
    passed = failed = 0

    def check(cond: bool, label: str) -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {label}")
        else:
            failed += 1
            print(f"  FAIL {label}")

    print("[0] 基线数据集应完全合规")
    fx = build()
    rules, violations, skipped = run(fx)
    errors = [x for x in violations if x.level == "error"]
    warns = [x for x in violations if x.level == "warning"]
    if errors:
        for x in errors:
            print(f"       R{x.rule} {x.obj}: {x.message} 例:{x.samples[:3]}")
    if warns:
        for x in warns:
            print(f"       W R{x.rule} {x.obj}: {x.message}")
    check(not errors, "合规数据集 0 error")
    check(not warns, f"合规数据集 0 warning（实际 {len(warns)}）")
    check({s["rule"] for s in skipped} == {25, 49}, f"仅 25/49 因缺基线跳过（实际 {sorted(s['rule'] for s in skipped)}）")

    print("\n[0b] 磁盘装载路径")
    _, file_violations = run_via_files(build())
    file_errors = [x for x in file_violations if x.level == "error"]
    check(not file_errors, "经 JSON 文件装载后仍 0 error")

    print("\n[1] 52 条规则逐条验活")
    baseline_fx = build()
    for rule, label, mutate, mode in cases():
        fx = build()
        mutate(fx)
        base = baseline_fx if mode == "baseline" else None
        fired, vlist, _ = run(fx, baseline=base)
        check(rule in fired, f"R{rule:<2} {label}")

    print("\n[2] 关键负向：合规数据不应被误判")
    fx = build()
    find(fx["daily"], ts_code="600519.SH", price_adjust="hfq").__setitem__("turnover_rate", 46.0)
    fired, _, _ = run(fx)
    check(34 not in fired, "换手率 46%（合法）不报量纲错误")

    fx = build()
    fx["limit_up"][0]["consecutive_boards"] = 5
    fired, _, _ = run(fx)
    check(21 not in fired, "连板数 5（合法）不报错")

    fx = build()
    fx["daily"][0]["price_basis"] = "synthetic"
    fired, _, _ = run(fx)
    check(15 in fired and 12 not in fired, "合成价只报 R15 不报 R12")

    # R7 的基准必须是"该证券自己的首根日 K"，不能是全市场最早日 K。
    # 2026-10-25 用真实数据跑出的误报：15 只窗口内上市的新股全部被全局基准判为 error。
    fx = build()
    fx["security"].append({"ts_code": "600002.SH", "symbol": "600002", "name": "新股示例",
                           "exchange": "SH", "market": "沪市主板", "list_date": "2026-10-20",
                           "list_status": "L"})
    fired, _, _ = run(fx)
    check(7 not in fired, "窗口内上市且尚无日 K 的新股不报 R7（基准逐证券）")

    fx = build()
    find(fx["security"], ts_code="600519.SH")["list_date"] = "2026-10-20"
    fired, _, _ = run(fx)
    check(7 in fired, "日 K 早于该证券上市日 → R7 报错")

    print()
    print("=" * 60)
    print(f"结果：PASS {passed} ｜ FAIL {failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
