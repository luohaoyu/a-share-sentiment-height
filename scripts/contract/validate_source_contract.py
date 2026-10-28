#!/usr/bin/env python3
"""原始数据契约校验器：把《原始数据字段规范 v1.1》第 8 节的 52 条规则变成可执行检查。

与 validate_contract.py 的区别：
    validate_contract.py       校验报告产物 data.json（下游契约）
    validate_source_contract.py 校验原始数据对象（上游契约，本文件）

设计原则：
1. 只校验契约形态的数据（一行一口径的展开形态），不解析旧缓存。
2. 规则编号与文档严格对应，报告直接给规则号，便于回查条文。
3. 无法执行的规则必须显式列在 skipped 里，禁止静默通过。

用法：
    python validate_source_contract.py --data-dir ./run/contract
    python validate_source_contract.py --data-dir ./run/contract --baseline-dir ./run/prev
    python validate_source_contract.py --data-dir ./run/contract --json-out report.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

MARKETS = {"沪市主板", "深市主板", "创业板", "科创板", "北交所"}
EXCHANGE_OF = {"沪市主板": "SH", "科创板": "SH", "深市主板": "SZ", "创业板": "SZ", "北交所": "BJ"}
ADJUSTS = {"hfq", "qfq", "none"}
LIST_STATUS = {"L", "D", "P"}
POOL_STATUS = {"ok", "error", "not_collected"}
RISK_COVERAGE = {"ok", "partial", "not_collected"}
OBJECTS = {"calendar", "security", "daily_kline", "limit_up_history", "risk_status"}
OBJECT_ATTR = {
    "calendar": "calendar",
    "security": "security",
    "daily_kline": "daily",
    "limit_up_history": "limit_up",
    "risk_status": "risk",
}
OBJECT_DATE_FIELD = {
    "calendar": "cal_date",
    "security": "list_date",
    "daily_kline": "trade_date",
    "limit_up_history": "trade_date",
    "risk_status": "trade_date",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TS_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
SYMBOL_RE = re.compile(r"^\d{6}$")
SID_RE = re.compile(r"^([A-Za-z0-9_]+):(hfq|qfq|none):([A-Za-z0-9_]+)$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
RAW_HHMMSS_RE = re.compile(r"^\d{6}$")

FILES = {
    "calendar": "calendar.json",
    "security": "security.json",
    "daily": "daily_kline.json",
    "limit_up": "limit_up_history.json",
    "limit_up_days": "limit_up_days.json",
    "risk": "risk_status.json",
    "capability": "source_capability.json",
    "manifest": "manifest.json",
}


class Dataset:
    """契约形态的一份数据集。"""

    def __init__(self) -> None:
        self.calendar: list[dict] = []
        self.security: list[dict] = []
        self.daily: list[dict] = []
        self.limit_up: list[dict] = []
        self.limit_up_days: list[dict] = []
        self.risk: list[dict] = []
        self.capability: list[dict] = []
        self.manifest: dict = {}
        self.present: set[str] = set()

    def rows(self, object_name: str) -> list[dict]:
        return getattr(self, OBJECT_ATTR[object_name])

    def open_days(self) -> set[str]:
        return {r["cal_date"] for r in self.calendar if _int(r.get("is_open")) == 1}

    def as_of(self) -> str | None:
        v = self.manifest.get("as_of")
        return v if isinstance(v, str) else None


class Violation:
    __slots__ = ("rule", "level", "obj", "message", "samples")

    def __init__(self, rule: int, level: str, obj: str, message: str, samples: list[str] | None = None):
        self.rule = rule
        self.level = level
        self.obj = obj
        self.message = message
        self.samples = samples or []

    def as_dict(self) -> dict:
        return {"rule": self.rule, "level": self.level, "object": self.obj,
                "message": self.message, "samples": self.samples[:5]}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or not DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _sid(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, str):
        return None
    m = SID_RE.match(value)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sample(rows: list[dict], field: str = "ts_code") -> list[str]:
    out = []
    for r in rows:
        key = r.get(field)
        if key is None:
            key = r.get("cal_date")
        out.append(str(key))
        if len(out) >= 5:
            break
    return out


def load_rows(path: Path) -> list[dict]:
    """支持 list / {"rows": [...]} / .jsonl 三种形态。"""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("rows", "records", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    raise ValueError(f"无法解析 {path}")


def load_dataset(data_dir: Path) -> Dataset:
    ds = Dataset()
    for attr, name in FILES.items():
        path = data_dir / name
        if not path.exists():
            continue
        if attr == "manifest":
            ds.manifest = json.loads(path.read_text(encoding="utf-8"))
        else:
            setattr(ds, attr, load_rows(path))
        ds.present.add(attr)
    return ds


def validate(ds: Dataset, baseline: Dataset | None = None, now: datetime | None = None):
    now = now or datetime.now()
    v: list[Violation] = []
    skipped: list[dict] = []
    open_days = ds.open_days()
    as_of = ds.as_of()
    sec_index = {r.get("ts_code") for r in ds.security}

    def skip(rule: int, reason: str) -> None:
        skipped.append({"rule": rule, "reason": reason})

    # ---------------- 1. 交易日历（规则 1、2、29、30） ----------------
    seen: set[str] = set()
    bad_fmt, bad_order, bad_open = [], [], []
    prev = None
    for r in ds.calendar:
        d = r.get("cal_date")
        if not _is_date(d):
            bad_fmt.append(str(d))
            continue
        if d in seen:
            bad_order.append(f"重复 {d}")
        seen.add(d)
        if prev is not None and d <= prev:
            bad_order.append(f"乱序 {prev} -> {d}")
        prev = d
        if _int(r.get("is_open")) not in (0, 1):
            bad_open.append(str(d))
    if bad_fmt:
        v.append(Violation(1, "error", "calendar", "cal_date 格式非法", bad_fmt[:5]))
    if bad_order:
        v.append(Violation(1, "error", "calendar", "cal_date 非唯一或非严格升序", bad_order[:5]))
    if bad_open:
        v.append(Violation(2, "error", "calendar", "is_open 不属于 {0,1}", bad_open[:5]))
    non_str = [str(r.get("cal_date")) for r in ds.calendar if not isinstance(r.get("cal_date"), str)]
    if non_str:
        v.append(Violation(30, "error", "calendar", "cal_date 残留非字符串（date 对象）", non_str[:5]))
    if as_of:
        future = [r["cal_date"] for r in ds.calendar
                  if _is_date(r.get("cal_date")) and r["cal_date"] > as_of]
        if future:
            v.append(Violation(29, "error", "calendar", f"cal_date 晚于采集日 {as_of}", sorted(future)[:5]))
    elif ds.calendar:
        skip(29, "manifest.as_of 缺失，无法判断未来日期")

    # ---------------- 2. 市场股票（规则 3~8、31、32） ----------------
    codes: set[str] = set()
    dup, bad_ts, bad_sym, bad_market, bad_ex, bad_status, bad_list = [], [], [], [], [], [], []
    bad_name, bad_list_type = [], []
    for r in ds.security:
        ts, sym = r.get("ts_code"), r.get("symbol")
        if not isinstance(ts, str) or not TS_RE.match(ts):
            bad_ts.append(str(ts))
        elif ts in codes:
            dup.append(ts)
        else:
            codes.add(ts)
        if not isinstance(sym, str) or not SYMBOL_RE.match(sym):
            bad_sym.append(f"{ts}:{sym}")
        elif isinstance(ts, str) and ts[:6] != sym:
            bad_sym.append(f"{ts} != {sym}")
        market, exchange = r.get("market"), r.get("exchange")
        if market not in MARKETS:
            bad_market.append(f"{ts}:{market}")
        elif EXCHANGE_OF.get(market) != exchange:
            bad_ex.append(f"{ts}:{market}/{exchange}")
        if r.get("list_status") not in LIST_STATUS:
            bad_status.append(f"{ts}:{r.get('list_status')}")
        ld = r.get("list_date")
        if ld is not None:
            if not isinstance(ld, str):
                bad_list_type.append(ts)
            elif not _is_date(ld):
                bad_list.append(f"{ts}:{ld}")
        name = r.get("name")
        if not isinstance(name, str) or not name.strip() or "  " in name:
            bad_name.append(f"{ts}:{name!r}")
    if bad_ts:
        v.append(Violation(3, "error", "security", "ts_code 格式非法", bad_ts[:5]))
    if dup:
        v.append(Violation(3, "error", "security", "ts_code 重复", dup[:5]))
    if bad_sym:
        v.append(Violation(4, "error", "security", "symbol 非 6 位或与 ts_code 前 6 位不符", bad_sym[:5]))
    if bad_market:
        v.append(Violation(5, "error", "security", "market 不在五值闭集", bad_market[:5]))
    if bad_ex:
        v.append(Violation(6, "error", "security", "exchange 与 market 映射不一致", bad_ex[:5]))
    if bad_status:
        v.append(Violation(8, "error", "security", "list_status 不属于 {L,D,P}", bad_status[:5]))
    if bad_list_type:
        v.append(Violation(31, "error", "security", "list_date 不是字符串", bad_list_type[:5]))
    if bad_list:
        v.append(Violation(7, "error", "security", "list_date 非合法日期", bad_list[:5]))
    if bad_name:
        v.append(Violation(32, "warning", "security", "name 为空或含连续空格", bad_name[:5]))

    # 规则 7：逐只比较。拿全市场最早一根日 K 做基准，会把"窗口内新上市的股票"全部误判——
    # 真实数据里 15 只 9/18 之后上市的新股就是这样被误报的（2026-10-25 实测）。
    if ds.daily and ds.security:
        first_by_code: dict[str, str] = {}
        for r in ds.daily:
            td, ts = r.get("trade_date"), r.get("ts_code")
            if _is_date(td) and isinstance(ts, str):
                if ts not in first_by_code or td < first_by_code[ts]:
                    first_by_code[ts] = td
        late = [f"{r.get('ts_code')}:{r.get('list_date')}>首行{first_by_code[r['ts_code']]}"
                for r in ds.security
                if _is_date(r.get("list_date")) and r.get("ts_code") in first_by_code
                and r["list_date"] > first_by_code[r["ts_code"]]]
        if late:
            v.append(Violation(7, "error", "security",
                               "list_date 晚于该证券首根日 K（名录与行情自相矛盾）", late[:5]))

    # ---------------- 3. 股票日 K（规则 9~15、33~38） ----------------
    pk: set[tuple] = set()
    dup_pk, bad_adjust, bad_series, half, bad_price, bad_vol, bad_day = [], [], [], [], [], [], []
    bad_turnover, bad_date_type, bad_basis, synthetic = [], [], [], []
    series_by_adjust: dict[str, set[str]] = {}
    sid_mismatch: list[str] = []
    for r in ds.daily:
        td, ts, adj = r.get("trade_date"), r.get("ts_code"), r.get("price_adjust")
        if not isinstance(td, str):
            bad_date_type.append(f"{ts}:{td!r}")
        elif not _is_date(td):
            bad_day.append(f"{ts}:{td}")
        elif open_days and td not in open_days:
            bad_day.append(f"{ts}:{td} 非开市日")
        if adj is None or adj == "":
            bad_adjust.append(f"{ts}:{td} 口径未打标")
        elif adj not in ADJUSTS:
            bad_adjust.append(f"{ts}:{td}:{adj}")
        key = (td, ts, adj)
        if key in pk:
            dup_pk.append(str(key))
        pk.add(key)

        prices = [r.get("open"), r.get("high"), r.get("low"), r.get("close")]
        present = [p for p in prices if p is not None]
        if present and len(present) != 4:
            half.append(f"{ts}:{td}:{adj}")
        for name, val in zip(("open", "high", "low", "close"), prices):
            if val is None:
                continue
            num = _num(val)
            if num is None or num <= 0:
                bad_price.append(f"{ts}:{td}:{name}={val}")
        o, h, l, c = (_num(x) for x in prices)
        if None not in (o, h, c) and h < max(o, c) - 1e-9:
            bad_price.append(f"{ts}:{td}:high < max(open, close)")
        if None not in (l, o, c) and l > min(o, c) + 1e-9:
            bad_price.append(f"{ts}:{td}:low > min(open, close)")
        for name in ("volume", "amount"):
            val = r.get(name)
            if val is None:
                continue
            num = _num(val)
            if num is None or num < 0:
                bad_vol.append(f"{ts}:{td}:{name}={val}")
        tr = r.get("turnover_rate")
        if tr is not None:
            num = _num(tr)
            if num is None or num < 0 or num > 100:
                bad_turnover.append(f"{ts}:{td}:{tr}")
        if r.get("price_basis") not in ("real", "synthetic"):
            bad_basis.append(f"{ts}:{td}:{r.get('price_basis')}")
        elif r["price_basis"] == "synthetic":
            synthetic.append(f"{ts}:{td}")
        raw_sid = r.get("series_id")
        parsed = _sid(raw_sid)
        if parsed is None:
            bad_series.append(f"{ts}:{td}:{raw_sid!r}")
        else:
            src, sid_adj, _base = parsed
            if isinstance(adj, str) and sid_adj != adj:
                sid_mismatch.append(f"{ts}:{td}:series口径={sid_adj} != {adj}")
            if adj:
                series_by_adjust.setdefault(str(adj), set()).add(str(raw_sid))
            if r.get("source") is not None and str(src) != str(r.get("source")):
                sid_mismatch.append(f"{ts}:{td}:series源={src} != {r.get('source')}")
    if dup_pk:
        v.append(Violation(9, "error", "daily_kline", "主键重复", dup_pk[:5]))
    if bad_adjust:
        unmarked = [x for x in bad_adjust if "未打标" in x]
        illegal = [x for x in bad_adjust if "未打标" not in x]
        if unmarked:
            v.append(Violation(33, "error", "daily_kline", "price_adjust 未打标（口径未定）", unmarked[:5]))
        if illegal:
            v.append(Violation(10, "error", "daily_kline", "price_adjust 不在 {hfq,qfq,none}", illegal[:5]))
    if half:
        v.append(Violation(11, "error", "daily_kline", "同口径四价不全（半组）", half[:5]))
    if bad_price:
        v.append(Violation(12, "error", "daily_kline", "价格非正或 OHLC 关系不成立", bad_price[:5]))
    if bad_vol:
        v.append(Violation(13, "error", "daily_kline", "volume/amount 为负或非数值", bad_vol[:5]))
    if bad_day:
        v.append(Violation(14, "error", "daily_kline", "trade_date 非开市日或非法", bad_day[:5]))
    if bad_date_type:
        v.append(Violation(35, "error", "daily_kline", "trade_date 残留非字符串", bad_date_type[:5]))
    if bad_turnover:
        v.append(Violation(34, "error", "daily_kline", "turnover_rate 超出 0~100（量纲错误）", bad_turnover[:5]))
    if bad_basis:
        v.append(Violation(12, "error", "daily_kline", "price_basis 非法", bad_basis[:5]))
    if synthetic:
        ratio = len(synthetic) / max(len(ds.daily), 1)
        v.append(Violation(15, "warning", "daily_kline", f"synthetic 价格占比 {ratio:.1%}", synthetic[:5]))
    if bad_series:
        v.append(Violation(36, "error", "daily_kline", "series_id 缺失或格式非法", bad_series[:5]))
    multi = [f"{adj}:{sorted(s)}" for adj, s in series_by_adjust.items() if len(s) > 1]
    if multi:
        v.append(Violation(37, "error", "daily_kline", "同口径出现多个 series_id（跨源/跨基准拼接）", multi[:5]))
    if sid_mismatch:
        v.append(Violation(38, "error", "daily_kline", "series_id 与 price_adjust/source 不一致", sid_mismatch[:5]))

    # ---------------- 4. 涨停历史（规则 16~28） ----------------
    if ds.limit_up or ds.limit_up_days:
        up_pk: set[tuple] = set()
        dup_up, bad_pool, bad_up_day, ghost, bad_board, bad_time = [], [], [], [], [], []
        per_day_status: dict[str, set[str]] = {}
        per_day_count: dict[str, int] = {}
        for r in ds.limit_up:
            td, ts = r.get("trade_date"), r.get("ts_code")
            if (td, ts) in up_pk:
                dup_up.append(f"{td}:{ts}")
            up_pk.add((td, ts))
            ps = r.get("pool_status")
            if ps not in POOL_STATUS:
                bad_pool.append(f"{td}:{ps}")
            else:
                per_day_status.setdefault(str(td), set()).add(ps)
            if isinstance(td, str) and _is_date(td):
                if open_days and td not in open_days:
                    bad_up_day.append(f"{td} 非开市日")
            else:
                bad_up_day.append(f"{ts}:{td!r}")
            if ts not in sec_index:
                ghost.append(f"{td}:{ts}")
            cb = r.get("consecutive_boards")
            if cb is not None and (_int(cb) is None or _int(cb) < 1):
                bad_board.append(f"{ts}:{td}:{cb}")
            ot = r.get("open_times")
            if ot is not None and (_int(ot) is None or _int(ot) < 0):
                bad_board.append(f"{ts}:{td}:open_times={ot}")
            for label, val in (("first", r.get("first_seal_time")), ("last", r.get("last_seal_time"))):
                if val is None:
                    continue
                if RAW_HHMMSS_RE.match(str(val)):
                    bad_time.append(f"{ts}:{td}:{label}={val} 残留 6 位原串")
                elif not TIME_RE.match(str(val)):
                    bad_time.append(f"{ts}:{td}:{label}={val}")
            f_time, l_time = r.get("first_seal_time"), r.get("last_seal_time")
            if (isinstance(f_time, str) and isinstance(l_time, str)
                    and TIME_RE.match(f_time) and TIME_RE.match(l_time) and f_time > l_time):
                bad_time.append(f"{ts}:{td}:首封 {f_time} 晚于末封 {l_time}")
            per_day_count[str(td)] = per_day_count.get(str(td), 0) + 1
        if dup_up:
            v.append(Violation(16, "error", "limit_up_history", "主键重复", dup_up[:5]))
        if bad_pool:
            v.append(Violation(17, "error", "limit_up_history", "pool_status 非法", bad_pool[:5]))
        if bad_up_day:
            v.append(Violation(18, "error", "limit_up_history", "trade_date 非开市日", bad_up_day[:5]))
        if ghost:
            v.append(Violation(19, "error", "limit_up_history", "ts_code 不存在于市场股票", ghost[:5]))
        mixed = [f"{d}:{sorted(s)}" for d, s in per_day_status.items() if len(s) > 1]
        if mixed:
            v.append(Violation(20, "error", "limit_up_history", "同日 pool_status 不一致", mixed[:5]))
        if bad_board:
            v.append(Violation(21, "error", "limit_up_history", "连板数/炸板次数取值非法", bad_board[:5]))
        raw_time = [x for x in bad_time if "原串" in x]
        if raw_time:
            v.append(Violation(28, "error", "limit_up_history", "封板时间残留 6 位 HHMMSS 原串", raw_time[:5]))
        other_time = [x for x in bad_time if "原串" not in x]
        if other_time:
            v.append(Violation(22, "error", "limit_up_history", "封板时间非法", other_time[:5]))

        price_nonnull = [f"{r.get('ts_code')}:{r.get('trade_date')}" for r in ds.limit_up
                         if r.get("limit_price") is not None]
        if price_nonnull:
            v.append(Violation(27, "warning", "limit_up_history", "limit_price 非空，疑似自行推算", price_nonnull[:5]))

        if ds.limit_up_days:
            mismatched = []
            for m in ds.limit_up_days:
                d = m.get("trade_date")
                cnt = _int(m.get("count"))
                actual = per_day_count.get(str(d), 0)
                if cnt is not None and cnt != actual:
                    mismatched.append(f"{d}:声明 {cnt} 实际 {actual}")
            if mismatched:
                v.append(Violation(23, "error", "limit_up_history", "count 与当日记录数不一致", mismatched[:5]))
        else:
            skip(23, "缺少 limit_up_days.json（整池级标记），无法核对 count")

        none_close = {(r.get("trade_date"), r.get("ts_code")): _num(r.get("close"))
                      for r in ds.daily
                      if r.get("price_adjust") == "none" and _num(r.get("close")) is not None}
        drift = []
        for r in ds.limit_up:
            c = _num(r.get("close"))
            ref = none_close.get((r.get("trade_date"), r.get("ts_code")))
            if c is not None and ref is not None and abs(c - ref) > 0.01:
                drift.append(f"{r.get('ts_code')}:{r.get('trade_date')} {c} != {ref}")
        if drift:
            v.append(Violation(24, "warning", "limit_up_history", "涨停收盘价与日 K none 口径不一致", drift[:5]))
    else:
        for rule in (16, 17, 18, 19, 20, 21, 22, 23, 24, 27, 28):
            skip(rule, "未提供涨停历史数据")

    # ---------------- 5. 风险状态（规则 39~44） ----------------
    if ds.risk:
        rk: set[tuple] = set()
        dup_r, bad_flag, bad_type, partial_zero, backfill, bad_risk_day = [], [], [], [], [], []
        for r in ds.risk:
            td, ts = r.get("trade_date"), r.get("ts_code")
            if (td, ts) in rk:
                dup_r.append(f"{td}:{ts}")
            rk.add((td, ts))
            flag = r.get("risk_flag")
            fint = None if flag is None else _int(flag)
            if flag is not None and fint not in (0, 1):
                bad_flag.append(f"{ts}:{td}:{flag}")
            if fint == 1 and not r.get("risk_type"):
                bad_type.append(f"{ts}:{td}:flag=1 但 risk_type 空")
            if fint == 0 and r.get("risk_type"):
                bad_type.append(f"{ts}:{td}:flag=0 但 risk_type={r.get('risk_type')}")
            if r.get("coverage_status") == "partial" and fint == 0 and r.get("name_snapshot") is None:
                partial_zero.append(f"{ts}:{td}")
            if r.get("coverage_status") not in RISK_COVERAGE:
                bad_flag.append(f"{ts}:{td}:coverage={r.get('coverage_status')}")
            if r.get("source") == "name_snapshot" and as_of and isinstance(td, str) and td < as_of:
                if r.get("coverage_status") != "partial":
                    backfill.append(f"{ts}:{td}:coverage={r.get('coverage_status')}")
            if (not (isinstance(td, str) and _is_date(td))
                    or (open_days and td not in open_days) or ts not in sec_index):
                bad_risk_day.append(f"{ts}:{td}")
        if dup_r:
            v.append(Violation(39, "error", "risk_status", "主键重复", dup_r[:5]))
        if bad_flag:
            v.append(Violation(40, "error", "risk_status", "risk_flag / coverage_status 取值非法", bad_flag[:5]))
        if bad_type:
            v.append(Violation(41, "error", "risk_status", "risk_flag 与 risk_type 不自洽", bad_type[:5]))
        if partial_zero:
            v.append(Violation(42, "error", "risk_status", "partial 覆盖下把未覆盖记为 0（正常）", partial_zero[:5]))
        if backfill:
            v.append(Violation(43, "error", "risk_status", "名称快照回填历史日期却未标 partial", backfill[:5]))
        if bad_risk_day:
            v.append(Violation(44, "error", "risk_status", "trade_date 或 ts_code 不合规", bad_risk_day[:5]))
    else:
        for rule in (39, 40, 41, 42, 43, 44):
            skip(rule, "未提供风险状态数据")

    # ---------------- 6. 能力声明（规则 45~52，含 26、47、48） ----------------
    if ds.capability:
        cpk: set[tuple] = set()
        dup_c, bad_obj, bad_windows, stale, fallback_bad = [], [], [], [], []
        for r in ds.capability:
            key = (r.get("source_id"), r.get("object_name"))
            if key in cpk:
                dup_c.append(str(key))
            cpk.add(key)
            if r.get("object_name") not in OBJECTS:
                bad_obj.append(f"{key}:object_name 非法")
            for label in ("window_start", "window_end"):
                val = r.get(label)
                if val is not None and not _is_date(val):
                    bad_windows.append(f"{key}:{label}={val}")
            probed = _parse_iso(r.get("probed_at"))
            if probed is None:
                bad_windows.append(f"{key}:probed_at 缺失或非法")
            elif (now - probed.replace(tzinfo=None)).days > 90:
                stale.append(f"{key}:probed_at={r.get('probed_at')}")

            obj = r.get("object_name")
            if obj not in OBJECTS:
                continue
            rows = ds.rows(obj)
            support = r.get("field_support") or {}
            fallback = r.get("field_fallback") or {}
            for field, level in support.items():
                if level == "absent" and field not in fallback:
                    offenders = [x for x in rows if x.get(field) is not None]
                    if offenders:
                        v.append(Violation(46, "error", obj,
                                           f"{key} 声明 {field}=absent 但实际有值", _sample(offenders)))
                if level == "derived":
                    offenders = [x for x in rows if x.get(field) is not None and x.get("source") != "derived"]
                    if offenders:
                        v.append(Violation(51, "error", obj,
                                           f"{key} 声明 {field}=derived 但 source 未标 derived",
                                           _sample(offenders)))
            for field, spec in fallback.items():
                if not isinstance(spec, dict) or spec.get("mode") != "constant":
                    fallback_bad.append(f"{key}:{field} 兜底模式不支持")
                    continue
                expected = spec.get("value")
                offenders = [x for x in rows if x.get(field) != expected]
                if rows and offenders:
                    v.append(Violation(52, "error", obj,
                                       f"{key} 声明 {field} 兜底常量 {expected!r}，实际不符",
                                       _sample(offenders)))
                if not spec.get("degrade"):
                    fallback_bad.append(f"{key}:{field} 缺少 degrade 说明")
        if dup_c:
            v.append(Violation(45, "error", "source_capability", "主键重复", dup_c[:5]))
        if bad_obj:
            v.append(Violation(45, "error", "source_capability", "object_name 不在闭集", bad_obj[:5]))
        if bad_windows:
            v.append(Violation(48, "error", "source_capability", "窗口或 probed_at 格式非法", bad_windows[:5]))
        if fallback_bad:
            v.append(Violation(52, "error", "source_capability", "field_fallback 声明不完整", fallback_bad[:5]))
        if stale:
            v.append(Violation(50, "warning", "source_capability", "能力声明超过 90 天未复核", stale[:5]))

        by_obj: dict[str, list[dict]] = {}
        for r in ds.capability:
            by_obj.setdefault(str(r.get("object_name")), []).append(r)
        for obj, rows_c in by_obj.items():
            if obj not in OBJECTS or not rows_c:
                continue
            if all(_int(x.get("supported")) == 0 for x in rows_c):
                rows = ds.rows(obj)
                if rows:
                    v.append(Violation(47, "error", obj, "能力声明 supported=0 但存在记录", _sample(rows)))
            starts = [_ for _ in (x.get("window_start") for x in rows_c) if _is_date(_)]
            if not starts:
                continue
            bound = min(starts)
            field = OBJECT_DATE_FIELD[obj]
            early = [x for x in ds.rows(obj) if _is_date(x.get(field)) and str(x.get(field)) < bound]
            if early:
                v.append(Violation(48, "error", obj, f"出现早于 window_start={bound} 的记录", _sample(early)))

        up_rows = by_obj.get("limit_up_history", [])
        up_start = min([_ for _ in (x.get("window_start") for x in up_rows) if _is_date(_)], default=None)
        if not up_start:
            skip(26, "能力声明未给出 limit_up_history.window_start")
        elif not ds.limit_up_days:
            skip(26, "缺少 limit_up_days.json（整池级标记）")
        else:
            bad = [f"{m.get('trade_date')}:ok+空" for m in ds.limit_up_days
                   if _is_date(m.get("trade_date")) and str(m["trade_date"]) < up_start
                   and m.get("pool_status") == "ok" and _int(m.get("count")) == 0]
            if bad:
                v.append(Violation(26, "error", "limit_up_history",
                                   f"超出保留窗口（早于 {up_start}）却标 ok 且记录为空", bad[:5]))
    else:
        for rule in list(range(45, 53)):
            skip(rule, "未提供能力声明")

    # ---------------- 7. 需要历史基线的规则（25、49） ----------------
    if baseline is not None:
        if ds.limit_up and baseline.limit_up:
            old = {(r.get("trade_date"), r.get("ts_code")): r.get("fetched_at") for r in baseline.limit_up}
            changed = [f"{r.get('trade_date')}:{r.get('ts_code')}" for r in ds.limit_up
                       if (r.get("trade_date"), r.get("ts_code")) in old
                       and old[(r.get("trade_date"), r.get("ts_code"))] != r.get("fetched_at")]
            if changed:
                v.append(Violation(25, "warning", "limit_up_history", "历史日记录被覆盖改写", changed[:5]))
                revised = {str(x.get("object_name")) for x in ds.capability
                           if x.get("drift_policy") == "revised"}
                if "limit_up_history" in revised:
                    v.append(Violation(49, "warning", "limit_up_history",
                                       "声明 revised 却改写了历史日", changed[:5]))
            if "limit_up_history" not in {str(x.get("object_name")) for x in ds.capability
                                          if x.get("drift_policy") == "revised"}:
                skip(49, "能力声明未将 limit_up_history 标为 revised")
        else:
            skip(25, "无基线涨停数据可比")
            skip(49, "无基线涨停数据可比")
    else:
        skip(25, "未提供 --baseline-dir")
        skip(49, "未提供 --baseline-dir")

    for name in sorted({"calendar", "security", "daily"} - ds.present):
        skipped.append({"rule": 0, "reason": f"缺少 {FILES[name]}，相关规则未执行"})

    v.sort(key=lambda x: (x.rule, x.level))
    return v, skipped


def summarize(v: list[Violation]) -> tuple[int, int]:
    return (sum(1 for x in v if x.level == "error"),
            sum(1 for x in v if x.level == "warning"))


def render(v: list[Violation], skipped: list[dict]) -> str:
    errors, warns = summarize(v)
    lines = ["=" * 70,
             f"原始数据契约校验：error {errors} ｜ warning {warns} ｜ 未执行 {len(skipped)}",
             "=" * 70]
    if not v:
        lines.append("无违规。" if not skipped else f"无违规，但仍有 {len(skipped)} 条规则未执行——结论不完整。")
    for x in v:
        tag = "ERROR" if x.level == "error" else "WARN "
        lines.append(f"[{tag}] R{x.rule:<2} {x.obj}: {x.message}")
        if x.samples:
            lines.append(f"          例: {', '.join(x.samples[:5])}")
    if skipped:
        lines.append("-" * 70)
        lines.append("未执行：")
        for s in skipped:
            lines.append(f"  {'R' + str(s['rule']) if s['rule'] else '--'}: {s['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验原始数据是否符合字段规范 v1.1")
    ap.add_argument("--data-dir", required=True, help="契约形态数据目录")
    ap.add_argument("--baseline-dir", default=None, help="上一次快照，用于规则 25/49")
    ap.add_argument("--json-out", default=None, help="把报告写成 JSON")
    ap.add_argument("--now", default=None, help="当前时间（ISO 8601），用于规则 50")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"目录不存在：{data_dir}", file=sys.stderr)
        return 2
    ds = load_dataset(data_dir)
    baseline = load_dataset(Path(args.baseline_dir)) if args.baseline_dir else None
    now = _parse_iso(args.now) if args.now else datetime.now()

    violations, skipped = validate(ds, baseline=baseline, now=now)
    print(render(violations, skipped))

    if args.json_out:
        errors, warns = summarize(violations)
        Path(args.json_out).write_text(json.dumps({
            "data_dir": str(data_dir),
            "summary": {"errors": errors, "warnings": warns, "skipped": len(skipped)},
            "violations": [x.as_dict() for x in violations],
            "skipped": skipped,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 报告：{args.json_out}")

    return 1 if summarize(violations)[0] else 0


if __name__ == "__main__":
    raise SystemExit(main())
