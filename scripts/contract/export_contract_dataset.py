#!/usr/bin/env python3
"""把 fetch_akshare_data.py 的缓存投影成《原始数据字段规范 v1.1》的契约形态。

为什么需要这一层
----------------
缓存是"一行三组列"的折叠形态（``hfq_close`` + ``qfq_*`` + ``raw_close`` 挤在同一行），
契约是"一行一口径"的展开形态（主键含 ``price_adjust``）。两者语义等价但形状不同，
而 ``validate_source_contract.py`` 只认契约形态。本脚本就是这两者之间唯一的桥。

两种投影模式，用途不同
----------------------
``--mode raw``
    只做字段改名与单位换算，**不做任何语义修复**。目的是把"缓存 → 契约"的真实差距
    全部暴露出来，交给校验器逐条报出。

``--mode normalized``
    在 raw 之上执行契约要求的归一，每一步都写进 ``projection_log.json``：
      * 剔除晚于 ``--as-of`` 的日历行（缓存已在采集层截断，此处退化成幂等空操作）
    其余归一分支（涨停池 availability / pool_status）随 v4 采集层移除三方涨停池
    接口而废弃——``is_limit_up`` 现为 ``price_store`` 中 raw 行的派生字段，
    由 K 线按官方涨跌幅限制本地计算，无 pool_status 概念。

v4 缓存（price_store）
----------------------
采集层已把三个缺口堵在源头，raw 投影不再需要 ``--enrich-ohlc`` 或任何归一：
  1. bootstrap/waves 落 hfq 与 raw 两套四价，不再是只有收盘；
  2. ``is_limit_up`` 由 K 线按官方涨跌幅限制本地计算，随 raw 一并落盘，
     不再依赖 ``stock_zt_pool_em`` 三方涨停池；
  3. ``calendar.json`` 已按观测日（``--as-of``）截断，不再含未来交易日。
``--enrich-ohlc`` 仍保留，但只在读 v2 老缓存时才有意义。

用法
----
    python export_contract_dataset.py --cache-dir ./run/akshare-real/cache \
        --out-dir ./run/contract-raw --as-of 2026-10-23 --mode raw

    python export_contract_dataset.py --cache-dir ./run/akshare-real/cache \
        --out-dir ./run/contract-fixed --as-of 2026-10-23 --mode normalized \
        --enrich-ohlc --security-scope daily-union
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# 技能根目录靠 SKILL.md 定位，不依赖固定的目录层级
_HERE = Path(__file__).resolve().parent
SKILL_ROOT = next(
    (p for p in (_HERE, *_HERE.parents) if (p / "SKILL.md").exists()),
    _HERE.parents[1],
)
DEFAULT_CAPABILITY = SKILL_ROOT / "references" / "source_capability.akshare.json"


def price_views_from_store(cache: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """Fold a v4 price store into the folded-view shape this projection expects.

    v4 keeps only unadjusted prices plus one adjustment factor per session, so
    hfq and qfq are derived on read. Folding here keeps every rule below
    identical across cache generations -- the projection does not need to know
    whether it was handed a v3 ``symbol_cache/`` or a v4 ``price_store/``.
    """
    sys.path.insert(0, str(SKILL_ROOT / "scripts" / "fetch"))
    import prices_v4 as prices  # noqa: PLC0415 - optional path, keep import local

    views: list[tuple[str, list[dict[str, Any]]]] = []
    for code in prices.list_symbols(cache):
        store = prices.read_store(cache, code)
        if not store:
            continue
        views.append((code, prices.derive_rows(store)["view"]))
    return views

MARKET_GROUP = {
    "沪市主板": "沪深主板",
    "深市主板": "沪深主板",
    "科创板": "科创成长板",
    "创业板": "科创成长板",
    "北交所": None,
}
SUFFIX_OF_PREFIX = (("6", "SH"), ("0", "SZ"), ("3", "SZ"), ("9", "BJ"), ("8", "BJ"), ("4", "BJ"))


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def parse_price_mode(mode: Any) -> tuple[str, str]:
    """``akshare_sina_hfq`` → (``sina``, ``real``)；合成价 → (``derived``, ``synthetic``)。

    ``source`` 会被写进 ``series_id`` 的第一段，且校验规则 38 要求它与 ``source`` 字段一致，
    所以这里必须解析出真实来源，不能统一填 ``akshare``。
    """
    text = str(mode or "")
    if text.startswith("synthetic"):
        return "derived", "synthetic"
    match = re.match(r"^akshare_([a-z0-9]+)_hfq$", text)
    if match:
        return match.group(1), "real"
    return "unknown", "real"


def guess_suffix(symbol: str) -> str:
    for prefix, suffix in SUFFIX_OF_PREFIX:
        if symbol.startswith(prefix):
            return suffix
    return "SZ"


def series_id(source: str, adjust: str, base: str) -> str:
    return f"{source}:{adjust}:{base}"


# --------------------------------------------------------------------------- 投影


def project(cache: Path, out: Path, as_of: str, mode: str, enrich: bool,
            security_scope: str, capability: Path, risk_from_master: bool) -> dict[str, Any]:
    log: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cache_dir": str(cache),
        "as_of": as_of,
        "mode": mode,
        "enrich_ohlc": enrich,
        "security_scope": security_scope,
        "normalizations": [],
        "notes": [],
        "rows": {},
    }

    calendar_rows = read_json(cache / "calendar.json", [])
    master = read_json(cache / "stock_basic.json", [])
    sym2ts = {row["symbol"]: row["ts_code"] for row in master if row.get("symbol")}
    ts2exchange = {row["ts_code"]: row.get("exchange") for row in master}

    # ---- 交易日历 -----------------------------------------------------------
    calendar = [{"cal_date": row["cal_date"], "is_open": int(row.get("is_open", 1))}
                for row in calendar_rows if row.get("cal_date")]
    if mode == "normalized":
        future = [row for row in calendar if row["cal_date"] > as_of]
        if future:
            calendar = [row for row in calendar if row["cal_date"] <= as_of]
            log["normalizations"].append(
                f"日历剔除晚于 as_of={as_of} 的 {len(future)} 行（上游为全年预排表，含未来交易日）")
    dump_json(out / "calendar.json", calendar)
    log["rows"]["calendar"] = len(calendar)

    # ---- 个股视图 → 日 K（一行一口径） --------------------------------------
    daily: list[dict[str, Any]] = []
    adjust_seen: dict[str, set[str]] = {}
    views: list[tuple[str, list[dict[str, Any]]]] = []
    view_paths = sorted((cache / "symbol_cache").glob("*.json"))
    if view_paths:
        for view_path in view_paths:
            payload = read_json(view_path, {})
            views.append((payload.get("ts_code") or view_path.stem, payload.get("rows") or []))
        log["notes"].append(f"价格视图：symbol_cache（v3 折叠视图，{len(views)} 只）")
    elif (cache / "price_store").is_dir():
        views = price_views_from_store(cache)
        log["notes"].append(
            f"价格视图：price_store（v4，{len(views)} 只；hfq = raw × 因子，qfq = hfq / 锚点因子，"
            "均按行派生，缓存内无重复价格）")
    for ts_code, view_rows in views:
        for row in view_rows:
            trade_date = row.get("trade_date")
            if not trade_date:
                continue
            source, basis = parse_price_mode(row.get("price_mode"))
            if row.get("hfq_close") is not None:
                daily.append({
                    "trade_date": trade_date, "ts_code": ts_code, "price_adjust": "hfq",
                    "open": row.get("hfq_open"), "high": row.get("hfq_high"),
                    "low": row.get("hfq_low"), "close": row.get("hfq_close"),
                    "price_basis": basis, "source": source,
                    "series_id": series_id(source, "hfq", "akshare"),
                })
            if row.get("qfq_close") is not None:
                daily.append({
                    "trade_date": trade_date, "ts_code": ts_code, "price_adjust": "qfq",
                    "open": row.get("qfq_open"), "high": row.get("qfq_high"),
                    "low": row.get("qfq_low"), "close": row.get("qfq_close"),
                    "price_basis": "real", "source": source,
                    "series_id": series_id(source, "qfq", "akshare"),
                })
            if row.get("raw_close") is not None:
                daily.append({
                    "trade_date": trade_date, "ts_code": ts_code, "price_adjust": "none",
                    "open": row.get("raw_open"), "high": row.get("raw_high"),
                    "low": row.get("raw_low"), "close": row.get("raw_close"),
                    "price_basis": "real", "source": source,
                    "series_id": series_id(source, "none", "akshare"),
                })
    for row in daily:
        adjust_seen.setdefault(row["price_adjust"], set()).add(row["series_id"])
    mixed = {adj: sorted(s) for adj, s in adjust_seen.items() if len(s) > 1}
    if mixed:
        log["notes"].append(
            "同一口径出现多个 series_id（合成价与真实价混排，契约第 3.7 条判为跨基准拼接）："
            + json.dumps(mixed, ensure_ascii=False))

    if enrich:
        daily = enrich_ohlc(daily, ts2exchange, log, out / "ohlc_cache.json")

    dump_json(out / "daily_kline.json", daily)
    log["rows"]["daily_kline"] = len(daily)
    log["rows"]["daily_by_adjust"] = {
        adj: sum(1 for r in daily if r["price_adjust"] == adj) for adj in ("hfq", "qfq", "none")}
    log["rows"]["daily_basis"] = {
        b: sum(1 for r in daily if r["price_basis"] == b) for b in ("real", "synthetic")}

    # ---- 市场股票 -----------------------------------------------------------
    live_symbols = {row["ts_code"] for row in daily}
    if security_scope == "daily-union":
        # is_limit_up 现是 price_store 中 raw 行的派生字段，与日K同源；命中涨停的
        # 股票必然已出现在 daily 中，故 union 退化为 live_symbols。
        keep = live_symbols
        security = [row for row in master if row.get("ts_code") in keep]
        log["normalizations"].append(
            f"security 限定为日K覆盖的 {len(security)} 只（原始名录 {len(master)} 只）；"
            "is_limit_up 为派生字段，涨停股已含于日K，无需再并涨停池")
    else:
        security = [{
            "ts_code": row.get("ts_code"),
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "exchange": row.get("exchange"),
            "market": row.get("market"),
            "list_date": row.get("list_date"),
            "list_status": row.get("list_status"),
            "market_group": MARKET_GROUP.get(str(row.get("market"))),
        } for row in master]
    dump_json(out / "security.json", security)
    log["rows"]["security"] = len(security)

    # ---- 涨停历史 + 整池标记（由 price_store 派生，不再读三方涨停池） ----------
    # is_limit_up 现是 price_store 中 raw 行的派生字段（按官方涨跌幅限制本地计算），
    # 只要 K 线齐全即可判定，无"fetch 失败 / 超出保留窗口"的概念；故 pool_status 恒为 ok。
    up_rows: list[dict[str, Any]] = []
    up_days: list[dict[str, Any]] = []
    per_day_count: dict[str, int] = {}
    for ts_code, view_rows in views:
        for row in view_rows:
            td = row.get("trade_date")
            if not td or row.get("is_limit_up") is not True:
                continue
            up_rows.append({
                "trade_date": td,
                "ts_code": ts_code,
                "pool_status": "ok",
                "close": row.get("raw_close"),
            })
            per_day_count[str(td)] = per_day_count.get(str(td), 0) + 1
    for td in sorted(per_day_count):
        up_days.append({
            "trade_date": td,
            "pool_status": "ok",
            "count": per_day_count[td],
        })
    dump_json(out / "limit_up_history.json", up_rows)
    dump_json(out / "limit_up_days.json", up_days)
    log["rows"]["limit_up_history"] = len(up_rows)
    log["rows"]["limit_up_days"] = len(up_days)
    log["rows"]["limit_up_status"] = {
        s: sum(1 for d in up_days if d["pool_status"] == s)
        for s in ("ok", "error", "not_collected")}

    # ---- 风险状态 -----------------------------------------------------------
    risk: list[dict[str, Any]] = []
    risk_files = sorted((cache / "st").glob("*.json"))
    for st_path in risk_files:
        for row in read_json(st_path, []) or []:
            if not row.get("ts_code"):
                continue
            risk.append({
                "trade_date": row.get("trade_date") or st_path.stem,
                "ts_code": row["ts_code"],
                "risk_flag": 1,
                "risk_type": "st_name",
                "name_snapshot": row.get("name"),
                "coverage_status": "partial",
                "source": "name_snapshot",
            })
    if risk:
        log["notes"].append(
            f"风险状态只有 {len(risk)} 条正样本（缓存只落名称命中行），无法表达"
            "「查过且不是 ST」的 risk_flag=0；契约要求的三值全量在这里表达不出来")
    else:
        log["notes"].append("缓存无 st/ 目录（bootstrap 不产风险状态，仅 snapshot 产出），规则 39~44 将跳过")

    if not risk and risk_from_master:
        # 名称快照是"当前"事实，但风险状态必须挂在开市日上：as_of 落在周末/节假日时
        # 必须回落到 <= as_of 的最后一个交易日，否则规则 44 判 trade_date 不合规。
        risk_date = calendar[-1]["cal_date"] if calendar else as_of
        if risk_date != as_of:
            log["notes"].append(
                f"as_of={as_of} 非开市日，名称快照风险状态改挂到最近交易日 {risk_date}（规则 44 要求开市日）")
        sec_index = {row["ts_code"] for row in security}
        for row in master:
            name = str(row.get("name") or "")
            compact = name.replace(" ", "")
            if row["ts_code"] in sec_index and ("ST" in compact or "退" in compact):
                risk.append({
                    "trade_date": risk_date,
                    "ts_code": row["ts_code"],
                    "risk_flag": 1,
                    "risk_type": "st_name",
                    "name_snapshot": name,
                    "coverage_status": "partial",
                    "source": "name_snapshot",
                })
        log["normalizations"].append(
            f"用采集日名称快照为 {len(risk)} 只风险名称证券生成 {risk_date} 的风险状态，"
            "按规则 43 标 coverage=partial（spot 不可用时的降级路径）")
    dump_json(out / "risk_status.json", risk)
    log["rows"]["risk_status"] = len(risk)

    # ---- manifest 与能力声明 ------------------------------------------------
    dump_json(out / "manifest.json", {
        "as_of": as_of,
        "schema_version": "contract-v1.1",
        "source": "akshare",
        "projection_mode": mode,
        "generated_at": log["generated_at"],
    })
    capability_payload = read_json(capability, None)
    if capability_payload is None:
        log["notes"].append(f"能力声明缺失：{capability}")
    else:
        dump_json(out / "source_capability.json", capability_payload)
        log["rows"]["source_capability"] = len(capability_payload) if isinstance(capability_payload, list) else 1

    dump_json(out / "projection_log.json", log)
    return log


def enrich_ohlc(daily: list[dict[str, Any]], ts2exchange: dict[str, Any],
                log: dict[str, Any], cache_file: Path | None = None) -> list[dict[str, Any]]:
    """按股补抓三套四价，只填空缺，不覆盖已有值。

    ``cache_file`` 存在时直接复用上次结果，避免重跑时再打 63 次上游。
    """
    if cache_file is not None and cache_file.exists():
        stored = read_json(cache_file, {})
        frames = {(ts, adj): table for (ts, adj), table in
                  ((k.split("|", 1), v) for k, v in (stored.get("frames") or {}).items())}
        frames = {(k[0], k[1]): {d: tuple(v) for d, v in table.items()} for k, table in frames.items()}
        if frames:
            log["normalizations"].append(f"四价复用本地缓存 {cache_file.name}（{len(frames)} 条序列）")
            return apply_ohlc(daily, frames, log)
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log["notes"].append(f"--enrich-ohlc 需要 akshare，导入失败：{exc}")
        return daily

    need = sorted({r["ts_code"] for r in daily})
    prefix_of = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
    dates = sorted({r["trade_date"] for r in daily})
    start, end = dates[0].replace("-", ""), dates[-1].replace("-", "")
    frames: dict[tuple[str, str], dict[str, tuple]] = {}
    failures: list[str] = []
    for ts_code in need:
        symbol = f"{prefix_of.get(str(ts2exchange.get(ts_code)), 'sh')}{ts_code[:6]}"
        for adjust in ("hfq", "qfq", ""):
            key = "none" if not adjust else adjust
            frame = None
            for _ in range(2):
                try:
                    frame = ak.stock_zh_a_daily(symbol=symbol, start_date=start,
                                                end_date=end, adjust=adjust)
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.8)
            if frame is None:
                failures.append(f"{ts_code}:{key}")
                continue
            table: dict[str, tuple] = {}
            date_col = "date" if "date" in frame.columns else "日期"
            for _, row in frame.iterrows():
                d = str(row[date_col])[:10]
                table[d] = (float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
            frames[(ts_code, key)] = table
            time.sleep(0.12)

    if cache_file is not None and frames:
        dump_json(cache_file, {"schema": "ohlc-cache-v1", "frames": {
            f"{ts}|{adj}": {d: list(v) for d, v in table.items()}
            for (ts, adj), table in frames.items()}})
    if failures:
        log["notes"].append(f"四价补抓失败 {len(failures)} 条：{', '.join(failures[:8])}")
    return apply_ohlc(daily, frames, log)


def apply_ohlc(daily: list[dict[str, Any]], frames: dict[tuple[str, str], dict[str, tuple]],
               log: dict[str, Any]) -> list[dict[str, Any]]:
    filled = 0
    for row in daily:
        table = frames.get((row["ts_code"], row["price_adjust"]))
        if not table:
            continue
        values = table.get(row["trade_date"])
        if not values:
            continue
        row["open"], row["high"], row["low"], row["close"] = values
        if row["price_adjust"] in ("hfq", "qfq"):
            row["price_basis"] = "real"
        filled += 1
    log["normalizations"].append(f"--enrich-ohlc 为 {filled} 行补齐四价（{len(frames)} 条序列）")
    return daily


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="缓存 → 原始数据契约形态投影")
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--as-of", required=True, help="采集/观测日 YYYY-MM-DD")
    ap.add_argument("--mode", choices=("raw", "normalized"), default="raw")
    ap.add_argument("--enrich-ohlc", action="store_true", help="网络：按股补抓四价")
    ap.add_argument("--security-scope", choices=("full", "daily-union"), default="full")
    ap.add_argument("--risk-from-master", action="store_true",
                    help="缓存无 st/ 时用名录名称快照生成风险状态（coverage=partial）")
    ap.add_argument("--capability", default=str(DEFAULT_CAPABILITY))
    args = ap.parse_args(argv)

    cache, out = Path(args.cache_dir), Path(args.out_dir)
    if not (cache / "calendar.json").exists():
        print(f"缓存不完整，缺 calendar.json：{cache}", file=sys.stderr)
        return 2
    log = project(cache, out, args.as_of, args.mode, args.enrich_ohlc,
                  args.security_scope, Path(args.capability), args.risk_from_master)
    print(json.dumps({"out_dir": str(out), "rows": log["rows"],
                      "normalizations": log["normalizations"],
                      "notes": log["notes"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
