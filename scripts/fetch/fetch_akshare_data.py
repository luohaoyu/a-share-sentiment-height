#!/usr/bin/env python3
"""Build and increment an A-share cache with AkShare.

One mode: bootstrap
    Fetch exchange stock masters in batches, then fetch a short, qfq (forward-
    adjusted) history per stock in a single request. This is the only expensive
    run and is resumable through per-symbol cache files.

    Increments are decided **per trading day** by :mod:`cache_refresh`: a symbol
    whose cached range already covers the requested window is skipped entirely
    (no network, no store rewrite); a symbol missing later sessions fetches just
    those sessions; a fully-unfetched symbol fetches the whole window. The "is
    today's bar already captured" gate compares ``last_update`` against the
    09:30 CST session-open so an intraday re-run does not re-pull the open bar.

    The snapshot and waves modes were folded into bootstrap: snapshot's
    whole-market append is just a bootstrap increment over the latest session,
    and waves' windowed candidate fetch is the same qfq request over a smaller
    window. Downstream consumers (compute_market_winners / screen /
    structure_waves / emotion_rallies) only ever divide one qfq close by
    another, so a single qfq series reproduces every downstream number.

is_limit_up is computed locally from each stock's K-line using the official
per-board price-limit ratio (主板 10% / 科创·创业板 20% / 北交所 30%) -- no
third-party 涨停池 interface is called.

The normalized output is consumed by compute_market_winners_from_daily.py with
--prices-adjusted. AkShare does not provide a reliable historical ST interval
in this workflow; current ST-name snapshots are audit aids, not a substitute
for historical risk-status data.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
from decimal import Decimal, ROUND_HALF_UP
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict, namedtuple
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

# 每只股日K取数任务：attempt 为本任务已尝试次数（0 起），tried_sources/reasons 累积历次失败信息。
_FetchTask = namedtuple("_FetchTask", ["stock", "attempt", "tried_sources", "reasons"])
# 任务级最多尝试次数：首投 + 2 次重投，共 3 次后仍失败则进入死信（dead_letter）。
MAX_TASK_ATTEMPTS = 3

# 中国标准时间（UTC+8，无夏令时）。水位/会话判定一律以它为准，避免部署到 UTC 容器时
# 北京时间 00:00-08:00 之间 date.today() 退回前一天导致判定错位一天。
CST = timezone(timedelta(hours=8))


def now_cst() -> datetime:
    return datetime.now(CST)


def date_today_cst() -> date:
    return datetime.now(CST).date()


def parse_iso_datetime(value: "str | None") -> "datetime | None":
    """Parse a stored ISO timestamp (naive local / CST wall clock) into a datetime.

    The price store persists ``last_update`` as ``datetime.isoformat(timespec="seconds")``,
    which is timezone-naive. Keeping it naive aligns with
    :func:`cache_refresh.session_open_cst` (also a naive 09:30) so the
    "is today's bar already captured" gate compares two wall-clock instants
    consistently instead of mixing UTC and CST.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    # 文档约定 last_update 为 naive 墙钟（与 session_open_cst 的 naive 09:30 对齐）；
    # store 实际以 datetime.now(CST).isoformat() 写入会带 +08:00，这里统一剥离时区，
    # 避免增量分支中 aware vs naive 比较崩溃。
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


# v4 price layer lives next to this file: raw prices + adjustment factors are
# the authority, every adjusted price is derived from them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import prices_v4  # noqa: E402
from cache_refresh import sessions_to_fetch  # noqa: E402

MARKET_SUFFIX = {
    "沪市主板": "SH",
    "科创板": "SH",
    "深市主板": "SZ",
    "创业板": "SZ",
    "北交所": "BJ",
}
RISK_PATTERN = re.compile(r"^(?:\*?ST|S\*ST)|退市整理|退市风险|终止上市", re.I)

# Cache contract version. Bumped when the on-disk schema changes.
#
# v5 (2026-10-26): qfq-only, single-request bootstrap, per-day incremental.
#   * ``price_store/<ts_code>.json`` holds exactly one qfq (forward-adjusted)
#     series plus ``last_update`` (ISO timestamp of the last write). The factor /
#     anchor / ex-rights-event machinery from v4 is gone: there is no second leg
#     to reconcile and no basis to drift on. Every adjusted price downstream is
#     derived by monotone rescaling of this one series, so the rescaling is a
#     no-op and the series *is* the answer.
#   * The fetch is a single ``adjust="qfq"`` request per source (sina /
#     eastmoney / tencent; tencent skipped for BJ). The downstream only consumes
#     close ratios, in which the adjustment basis cancels, so fetching qfq once
#     reproduces every downstream number bit-for-bit vs the v4 raw+hfq pair.
#   * Increments are decided per trading day by ``cache_refresh.sessions_to_fetch``:
#     a covered symbol is skipped; a partially-covered symbol fetches only the
#     missing sessions; an empty cache fetches the whole window. The "is today's
#     bar already captured" gate (``last_update`` vs 09:30 CST open) prevents an
#     intraday re-run from re-pulling the open bar.
#   * snapshot and waves modes were folded into bootstrap (see module docstring);
#     ``--mode`` is now constrained to ``bootstrap`` / ``status``.
#   * is_limit_up is computed locally from each stock's K-line using the official
#     per-board price-limit ratio (主板 10% / 科创·创业板 20% / 北交所 30%) -- no
#     third-party 涨停池 interface is called. ST 5% is not applied (no reliable
#     historical ST status in this workflow).
#   See scripts/fetch/prices_v4.py for the precision contract.
#
# v3 (2026-10-25): three upstream gaps closed. Additive but breaking for old
# caches, hence the bump:
#   * symbol_cache rows carry hfq_open/hfq_high/hfq_low and raw_open/raw_high/
#     raw_low; bootstrap/waves used to persist close only, which forced the
#     contract projection to re-fetch three series per symbol just to satisfy
#     the four-price rule;
#   * limit_up_pool/*.json carries an explicit three-valued ``pool_status``
#     (ok / not_collected / error) so "upstream kept nothing" can never be read
#     as "no limit-up that day";
#   * calendar.json is truncated at the observation date (``as_of``). The
#     upstream table is a full-year pre-schedule, so the persisted calendar used
#     to contain dozens of future sessions that can never have bars.
SCHEMA_VERSION = 5
# Concurrency model. The client no longer throttles requests: each data source paces
# itself and call_with_retry retries on rejection, so the only levers are the thread
# pool size and the source roster (DEFAULT_SOURCES_ENABLED). More workers overlap the
# ~1-2s per-call network latency across the three source pipelines; once a source is
# the latency ceiling, extra workers add no throughput. The default of 32 keeps all
# pipelines busy for a typical roster; WORKER_LIMIT caps the value a user may request.
WORKER_LIMIT = 64
# 进程分片上限。单进程受 GIL 限制，加线程收益递减；分片到多个 OS 进程才能真正吃满多核。
# 32 是保守上限（16 核机器上 8~10 片已足够），再高只会给上游加压而不提速。
SHARD_LIMIT = 32

# ---------------------------------------------------------------------------
# py_mini_racer 单例守卫：消除 V8 partition allocator 的并发初始化崩溃
# ---------------------------------------------------------------------------
# AkShare 的 stock_zh_a_daily() 每次调用都会 new 一个 MiniRacer() 来解码新浪的 JS
# 混淆行情。多线程并发构造时 V8 会在同一进程里二次初始化 partition allocator 并直接
# abort（进程级，Python 的 try/except 抓不住）：
#   [FATAL:partition_address_space.cc(243)] Check failed: !IsConfigurablePoolInitialized()
# 2026-10-26 的 8 进程分片实测中 8 个分片有 4 个因此中途死掉。改成全进程共享一个 V8
# 上下文、构造只发生一次、使用时加锁，从根上消除这个竞态。
_V8_LOCK = threading.Lock()
_V8_STATE: dict[str, Any] = {"ctx": None, "real": None}


class _SharedMiniRacer:
    """Drop-in replacement for ``py_mini_racer.MiniRacer``.

    Every instance is a handle onto one process-wide V8 context, and every
    ``eval`` / ``call`` runs under a module lock. Serialising the decode costs
    ~160ms per symbol (twice per symbol) -- cheap next to the HTTP legs it
    protects -- and it removes the concurrent-construction abort entirely.
    """

    def eval(self, *args: Any, **kwargs: Any) -> Any:
        with _V8_LOCK:
            return _V8_STATE["ctx"].eval(*args, **kwargs)

    def call(self, *args: Any, **kwargs: Any) -> Any:
        with _V8_LOCK:
            return _V8_STATE["ctx"].call(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        with _V8_LOCK:
            return getattr(_V8_STATE["ctx"], item)


def install_shared_v8() -> bool:
    """Route ``py_mini_racer`` through one locked V8 context (idempotent)."""
    try:
        import py_mini_racer
    except Exception:  # noqa: BLE001 - decoder optional; the sina leg fails on its own
        return False
    if _V8_STATE["ctx"] is not None:
        return True
    real = py_mini_racer.MiniRacer
    _V8_STATE["real"] = real
    with _V8_LOCK:
        if _V8_STATE["ctx"] is None:
            _V8_STATE["ctx"] = real()
    py_mini_racer.MiniRacer = _SharedMiniRacer
    return True
# is_limit_up 现为 price_store 中 raw 行的派生字段，由 K 线按官方涨跌幅限制本地
# 计算；旧的"涨停池三值状态"枚举（ok/not_collected/error）随三方涨停池接口一并废弃。


def import_dependencies() -> tuple[Any, Any]:
    try:
        import akshare as ak  # type: ignore
        import pandas as pd  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "missing dependency; install akshare and pandas in the managed Python venv"
        ) from exc
    return ak, pd


# Client-side rate limiting has been removed. Each data source paces itself and
# call_with_retry retries on rejection, so the client no longer caps request
# throughput. What remains is the *source roster*: a source is enabled when its
# flag is true. tencent additionally cannot serve 北交所 (handled in source_eligible);
# a disabled source is never assigned work.
#
# eastmoney 默认关闭（2026-10-26 实测）：单进程基线里它分到 30% 的任务、失败率
# 47.2%（1945 次尝试里挂 919 次）；8 分片高压下失败率 99.0%（881 次尝试只成 9 次），
# 错误清一色 RemoteDisconnected，且 p50 仅 0.56s —— 是"秒挂"不是超时。它不仅白占
# 权重，还靠重试把总请求量抬高约 16%。需要它时用 --source-rates 显式打开。
DEFAULT_SOURCES_ENABLED = {"sina": True, "eastmoney": False, "tencent": True}

# 三源权重：仅决定"按权重随机分配任务"的比例，与 --source-rates 的启用/停用正交。
# 新浪最稳、东财易限流（少给）、腾讯补位；可用 --source-weights 覆盖。
DEFAULT_SOURCE_WEIGHTS = {"sina": 5, "eastmoney": 3, "tencent": 2}


def source_eligible(stock: dict[str, Any], enabled: dict[str, bool]) -> list[str]:
    """Sources that can serve ``stock`` AND are enabled.

    tencent only covers 沪深 (not 北交所); a source whose flag is false is disabled.
    """
    candidates = ["sina", "eastmoney", "tencent"]
    if str(stock.get("exchange", "")).upper() == "BJ":
        candidates = ["sina", "eastmoney"]
    return [s for s in candidates if enabled.get(s, False)]


def pick_weighted_source(stock: dict[str, Any], enabled: dict[str, bool], weights: dict[str, float]) -> str:
    """Weighted-random pick of an *eligible* source for one stock.

    Eligibility (board coverage + enable flag) comes from :func:`source_eligible`
    (tencent does not cover 北交所). Among eligible sources the choice is weighted by
    ``weights`` (e.g. ``{"sina": 5, "eastmoney": 3, "tencent": 2}``) so the
    higher-weighted sources receive proportionally more tasks. A stock is retried
    as a fresh queue task on failure; :func:`pick_retry_source` prefers an untried
    eligible source before allowing a repeat.
    """
    elig = source_eligible(stock, enabled)
    if not elig:
        raise RuntimeError(f"no enabled source can serve {stock.get('ts_code')}")
    w = [max(0.0, float(weights.get(s, 0.0))) for s in elig]
    if sum(w) <= 0:
        w = [1.0] * len(elig)
    return random.choices(elig, weights=w, k=1)[0]


def pick_retry_source(
    stock: dict[str, Any],
    enabled: dict[str, bool],
    weights: dict[str, float],
    tried_sources: list[str],
) -> str:
    """Prefer a not-yet-tried eligible source for task-level retries.

    Weighted random assignment remains unchanged for the first attempt. On retry,
    repeatedly selecting the same failing adapter wastes the bounded attempt budget
    and can dead-letter a symbol even though another enabled adapter is healthy.
    After every eligible source has been tried, weighted selection is allowed again.
    """
    eligible = source_eligible(stock, enabled)
    untried = [source for source in eligible if source not in set(tried_sources)]
    if not untried:
        return pick_weighted_source(stock, enabled, weights)
    retry_enabled = {source: source in untried for source in enabled}
    return pick_weighted_source(stock, retry_enabled, weights)


def resolve_source_rates(args: "argparse.Namespace") -> dict[str, bool]:
    """Build the enabled-source roster from --source-rates / --rate-limit.

    The roster starts from :data:`DEFAULT_SOURCES_ENABLED` (eastmoney is off by
    default). --source-rates is a JSON map such as ``{"eastmoney": true}`` (or
    ``{"eastmoney": 1}``) that overrides individual sources; --rate-limit 0 is a
    kill switch that disables every source. --rate-limit > 0 is a no-op now: the
    global limiter it used to control has been removed, and it must not silently
    re-enable a source that the default roster deliberately disables.
    """
    enabled: dict[str, bool] = dict(DEFAULT_SOURCES_ENABLED)
    path = getattr(args, "source_rates", None)
    if path:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        for src, val in user.items():
            enabled[src] = bool(val)
    if getattr(args, "rate_limit", 0) == 0:
        enabled = {src: False for src in enabled}
    if not any(enabled.values()):
        raise ValueError("every data source is disabled; enable at least one via --source-rates")
    return enabled


def resolve_source_weights(args: "argparse.Namespace") -> dict[str, float]:
    """Build the per-source weight map from --source-weights.

    ``--source-weights`` is a JSON map such as ``{"sina": 5, "eastmoney": 3,
    "tencent": 2}``; any non-negative number sets that source's relative share of
    tasks. Weights are orthogonal to ``--source-rates`` (which only enables /
    disables a source): a disabled source receives no work regardless of weight.
    Missing sources fall back to :data:`DEFAULT_SOURCE_WEIGHTS`.
    """
    weights: dict[str, float] = dict(DEFAULT_SOURCE_WEIGHTS)
    path = getattr(args, "source_weights", None)
    if path:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        for src, val in user.items():
            weights[src] = float(val)
    return weights


def normalize_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10]).isoformat()


def symbol6(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def ts_code(symbol: str, market: str) -> str:
    return f"{symbol6(symbol)}.{MARKET_SUFFIX[market]}"


def json_ready(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        return json_ready(value.item())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def frame_records(frame: Any) -> list[dict[str, Any]]:
    return [{str(key): json_ready(value) for key, value in row.items()} for row in frame.to_dict("records")]


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically stream compact JSON for machine-consumed cache artifacts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    os.replace(temporary, path)


def read_json_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("rows", "records", "items"):
            if isinstance(value.get(key), list):
                return [row for row in value[key] if isinstance(row, dict)]
    return []


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def symbol_view_path(output_dir: Path, ts_code: str) -> Path:
    """Legacy v3 view path.

    v4 no longer writes ``symbol_cache/``: the folded view is derivable from the
    price store, so keeping it would reintroduce the duplication v4 removes. The
    path helper stays for replaying a v3 cache.
    """
    return output_dir / "symbol_cache" / f"{ts_code}.json"







def column(frame: Any, candidates: tuple[str, ...]) -> str:
    for name in candidates:
        if name in frame.columns:
            return name
    raise ValueError(f"missing required column; expected one of {candidates}, got {list(frame.columns)}")


def optional_column(frame: Any, candidates: tuple[str, ...]) -> "str | None":
    """Same as :func:`column` but returns ``None`` instead of raising.

    The three history adapters do not agree on column names (sina uses
    ``open/high/low/close``, eastmoney/tencent use 开盘/最高/最低/收盘, tencent
    omits some columns entirely), so four-price extraction has to degrade to
    nulls per column rather than reject the whole frame.
    """
    for name in candidates:
        if name in frame.columns:
            return name
    return None


DATE_COLUMNS = ("日期", "date", "trade_date")
OPEN_COLUMNS = ("开盘", "open")
HIGH_COLUMNS = ("最高", "high")
LOW_COLUMNS = ("最低", "low")
CLOSE_COLUMNS = ("收盘", "close")


def frame_ohlc_rows(
    frame: Any,
    ts_code: str,
    sessions: "set[str] | None" = None,
    price_mode: "str | None" = None,
) -> list[dict[str, Any]]:
    """Pull date + four prices out of any adapter frame, one row per session.

    Missing columns become ``None`` rather than an exception: a source that only
    exposes close still yields usable rows, and the four-price gap stays visible
    downstream instead of being silently filled with the close price.
    """
    date_col = column(frame, DATE_COLUMNS)
    open_col = optional_column(frame, OPEN_COLUMNS)
    high_col = optional_column(frame, HIGH_COLUMNS)
    low_col = optional_column(frame, LOW_COLUMNS)
    close_col = column(frame, CLOSE_COLUMNS)

    def value(row: Any, name: "str | None") -> "float | None":
        if name is None:
            return None
        try:
            number = float(row[name])
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        day = normalize_date(row[date_col])
        if sessions is not None and day not in sessions:
            continue
        entry = {
            "ts_code": ts_code,
            "trade_date": day,
            "open": value(row, open_col),
            "high": value(row, high_col),
            "low": value(row, low_col),
            "close": value(row, close_col),
        }
        if price_mode is not None:
            entry["price_mode"] = price_mode
        rows.append(entry)
    return rows


def needs_ohlc_backfill(rows: list[dict[str, Any]], sessions: list[str], fields: tuple[str, ...]) -> bool:
    """True when a cached pass covers ``sessions`` but stores close only.

    Caches written by the schema-v2 adapter (or by a fetch that predates the
    four-price change) satisfy the date-coverage check, so without this probe a
    re-run would keep the close-only rows forever and never repair them.
    """
    wanted = set(sessions)
    for row in rows:
        if row.get("trade_date") not in wanted:
            continue
        if any(row.get(field) is None for field in fields):
            return True
    return False


def call_with_retry(
    operation: Callable[[], Any],
    label: str,
    retries: int,
    min_interval: float = 0.0,
) -> tuple[Any, int, float]:
    """Call ``operation`` with bounded retries; the client no longer throttles.

    A source that rejects or rate-limits us is retried (exponential backoff, capped)
    rather than silently rerouted. ``min_interval`` is an optional fixed floor
    (seconds) added to the backoff between attempts; default 0 means no extra spacing.
    """
    started = time.perf_counter()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            result = operation()
            return result, attempt, time.perf_counter() - started
        except Exception as exc:  # noqa: BLE001 - remote source errors vary
            last_error = exc
            if attempt < retries:
                time.sleep(min(60.0, max(min_interval, 0.0) + 2.0 ** attempt + random.random()))
    raise RuntimeError(f"{label} failed after {retries} attempts: {last_error}")


def fetch_calendar(
    ak: Any,
    output_dir: Path,
    refresh: bool,
    as_of: "str | None" = None,
) -> tuple[list[str], "str | None"]:
    """Persist the trading calendar **truncated at the observation date**.

    ``tool_trade_date_hist_sina()`` returns a full-year *pre-schedule*: measured on
    2026-09-25 it ran to 2026-12-31, i.e. 49 sessions that cannot have any bars yet.
    Persisting it verbatim made downstream "future session" checks impossible and
    let a naive consumer treat un-occurred sessions as missing data.

    ``calendar.json`` therefore only keeps sessions ``<= as_of`` (the authoritative
    consumption view); the untouched upstream table is kept beside it in
    ``calendar_upstream.json`` for auditing and for the coverage message below.

    Returns ``(sessions, upstream_last)`` where ``sessions`` is sorted ascending and
    ``upstream_last`` is the last date the upstream table knows about (for error text).
    """
    as_of = as_of or date_today_cst().isoformat()
    path = output_dir / "calendar.json"
    upstream_path = output_dir / "calendar_upstream.json"
    fresh = refresh or not path.exists()
    truncated = fresh
    if fresh:
        frame = ak.tool_trade_date_hist_sina()
        date_col = column(frame, ("trade_date", "日期"))
        all_rows = sorted(
            ({"cal_date": normalize_date(value), "is_open": 1} for value in frame[date_col].tolist()),
            key=lambda row: row["cal_date"],
        )
        atomic_write_json(upstream_path, all_rows)
        rows = [row for row in all_rows if row["cal_date"] <= as_of]
    else:
        rows = read_json_rows(path)
        if any(normalize_date(row.get("cal_date") or row.get("trade_date")) > as_of for row in rows):
            # 幂等自愈：老缓存（v2 及以前）里有未来交易日，就地截断并留档原表
            if not upstream_path.exists():
                atomic_write_json(upstream_path, sorted(
                    rows, key=lambda row: normalize_date(row["cal_date"])))
            rows = [row for row in rows if normalize_date(row["cal_date"]) <= as_of]
            truncated = True
    if truncated:
        atomic_write_json(path, rows)
    sessions = sorted({normalize_date(row.get("cal_date") or row.get("trade_date")) for row in rows})
    upstream_last = None
    if upstream_path.exists():
        upstream_dates = sorted({
            normalize_date(row.get("cal_date")) for row in read_json_rows(upstream_path) if row.get("cal_date")
        })
        upstream_last = upstream_dates[-1] if upstream_dates else None
    return sessions, upstream_last


def append_master_rows(frame: Any, market: str, code_names: tuple[str, ...], name_names: tuple[str, ...], date_names: tuple[str, ...]) -> list[dict[str, Any]]:
    code_col = column(frame, code_names)
    name_col = column(frame, name_names)
    date_col = column(frame, date_names)
    output: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        code = symbol6(row[code_col])
        if not code:
            continue
        output.append({
            "ts_code": ts_code(code, market),
            "symbol": code,
            # 交易所把 3 字简称补空格到定宽（实测 52 只，如 '万  科Ａ'）。压成单空格：
            # 这是排版填充不是名称本身，连续空格会让下游名称匹配与展示都出错。
            "name": re.sub(r"\s+", " ", str(row[name_col])).strip(),
            "market": market,
            "exchange": MARKET_SUFFIX[market],
            "list_date": normalize_date(row[date_col]),
            "list_status": "L",
        })
    return output


def fetch_master(ak: Any, output_dir: Path, refresh: bool) -> list[dict[str, Any]]:
    path = output_dir / "stock_basic.json"
    if path.exists() and not refresh:
        return read_json_rows(path)
    rows: list[dict[str, Any]] = []
    sh_main = ak.stock_info_sh_name_code(symbol="主板A股")
    rows.extend(append_master_rows(sh_main, "沪市主板", ("证券代码",), ("证券简称", "公司简称"), ("上市日期",)))
    star = ak.stock_info_sh_name_code(symbol="科创板")
    rows.extend(append_master_rows(star, "科创板", ("证券代码",), ("证券简称", "公司简称"), ("上市日期",)))
    sz = ak.stock_info_sz_name_code(symbol="A股列表")
    board_col = column(sz, ("板块",))
    for board, market in (("主板", "深市主板"), ("创业板", "创业板")):
        subset = sz[sz[board_col].astype(str).str.contains(board, na=False)]
        rows.extend(append_master_rows(subset, market, ("A股代码", "证券代码"), ("A股简称", "证券简称"), ("A股上市日期", "上市日期")))
    bj = ak.stock_info_bj_name_code()
    rows.extend(append_master_rows(bj, "北交所", ("证券代码",), ("证券简称",), ("上市日期",)))
    deduplicated = {row["ts_code"]: row for row in rows}
    result = sorted(deduplicated.values(), key=lambda row: row["ts_code"])
    atomic_write_json(path, result)
    return result


def merge_partition(path: Path, rows: list[dict[str, Any]]) -> None:
    merged = {row["ts_code"]: row for row in read_json_rows(path) if row.get("ts_code")}
    for row in rows:
        merged[row["ts_code"]] = row
    atomic_write_json(path, sorted(merged.values(), key=lambda row: row["ts_code"]))


def market_prefix(stock: dict[str, Any]) -> str:
    exchange = str(stock.get("exchange", "")).upper()
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange, exchange.lower())


def fetch_qfq_frame(
    ak: Any,
    stock: dict[str, Any],
    start_date: str,
    end_date: str,
    retries: int,
    min_interval: float,
    primary_source: str = "sina",
) -> tuple[Any, str, int, float]:
    """Fetch the forward-adjusted (qfq) daily OHLC for one stock in a single request.

    v5 fetches qfq directly (``adjust="qfq"`` on every adapter) -- there is no
    second raw/hfq leg, because the downstream only consumes close ratios, which
    are basis-independent. The stock is pinned to its assigned primary source
    with no cross-source fallback; the worker retry / re-route handles failures.
    """
    symbol = stock["symbol"]
    builders: dict[str, Callable[[], Any]] = {
        "sina": lambda: ak.stock_zh_a_daily(
            symbol=f"{market_prefix(stock)}{symbol}",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        ),
        "eastmoney": lambda: ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
            timeout=30,
        ),
    }
    if stock.get("exchange") != "BJ":
        builders["tencent"] = lambda: ak.stock_zh_a_hist_tx(
            symbol=f"{market_prefix(stock)}{symbol}",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
            timeout=30,
        )
    source = f"{primary_source}_qfq"
    try:
        frame, attempts, elapsed = call_with_retry(
            builders[primary_source],
            f"{source}({symbol})",
            retries,
            min_interval,
        )
    except Exception as exc:  # noqa: BLE001 - surface the primary-source failure
        raise RuntimeError(f"{source}: {type(exc).__name__}: {exc}") from exc
    if frame is None or frame.empty:
        raise RuntimeError(f"{source}: empty qfq frame")
    return frame, source, attempts, elapsed




def bootstrap(
    ak: Any,
    output_dir: Path,
    master: list[dict[str, Any]],
    sessions: list[str],
    retries: int,
    min_interval: float,
    selected_symbols: set[str],
    workers: int = 1,
    enabled: "dict[str, bool] | None" = None,
    weights: "dict[str, float] | None" = None,
    partitions_out: "Path | None" = None,
    max_attempts: int = MAX_TASK_ATTEMPTS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start_date = sessions[0].replace("-", "")
    end_date = sessions[-1].replace("-", "")
    daily_dir = output_dir / "daily_adjusted"
    # is_limit_up 改为从 K 线按官方涨跌幅限制在前复权口径下本地计算（annotate_limit_up），
    # 不再依赖第三方涨停池接口；本函数不再引入实时会话覆盖逻辑。
    enabled = enabled or dict(DEFAULT_SOURCES_ENABLED)
    weights = weights or dict(DEFAULT_SOURCE_WEIGHTS)
    universe = [row for row in master if not selected_symbols or row["symbol"] in selected_symbols]
    if not universe:
        return [], []

    # "now" is the observation instant for the cache-refresh decision tree; it is
    # captured once per bootstrap run (CST) and closed over by fetch_one so every
    # symbol is judged against the same wall clock.
    now = now_cst()
    # One-time compatibility bridge for v5 stores written before requested coverage
    # was persisted. Only a successful prior bootstrap manifest may supply this
    # metadata; failed/partial runs are never treated as complete.
    legacy_manifest = prices_v4.load_json(output_dir / "bootstrap_manifest.json") or {}
    legacy_summary = legacy_manifest.get("summary") or {}
    legacy_coverage = legacy_summary.get("coverage") or {}
    legacy_ok = {
        str(item.get("ts_code"))
        for item in (legacy_manifest.get("requests") or [])
        if item.get("status") == "ok" and item.get("ts_code")
    }

    def fetch_one(stock: dict[str, Any], primary: str) -> tuple[dict[str, Any], list[dict[str, Any]]]: 
        """Fetch one stock's daily qfq K from a single pinned source.

        v5 fetches qfq directly. The cache-refresh decision
        (:func:`sessions_to_fetch`) compares the requested window against this
        symbol's cached range and returns only the missing trading sessions, so the
        increment is decided **per trading day** -- a fully covered symbol is
        skipped entirely (no network, no store rewrite). Missing days are fetched
        in one qfq request and merged into the qfq series; ``is_limit_up`` is
        recomputed locally from the qfq close ratio (basis-independent).
        """
        symbol = stock["symbol"]
        ts_code = stock["ts_code"]
        store = prices_v4.read_store(output_dir, ts_code) or {}
        qfq_by_date = {row["trade_date"]: dict(row) for row in store.get("qfq") or [] if row.get("trade_date")}
        observed_first, observed_last = prices_v4.cache_range(store)
        # Cache coverage means "this request window has been checked", not "a K-line
        # exists on both boundary dates". Newly listed/suspended symbols legitimately
        # lack rows on requested sessions; using observed bounds would re-fetch those
        # same empty ranges forever. Legacy stores fall back to observed bounds once.
        cache_first = store.get("requested_first") or observed_first
        cache_last = store.get("requested_last") or observed_last
        if (
            not store.get("requested_first")
            and ts_code in legacy_ok
            and legacy_coverage.get("first_session")
            and legacy_coverage.get("last_session")
        ):
            cache_first = str(legacy_coverage["first_session"])
            cache_last = str(legacy_coverage["last_session"])
        last_update = parse_iso_datetime(store.get("last_update"))
        missing = sessions_to_fetch(
            cache_first, cache_last, last_update, sessions[0], sessions[-1], now, sessions,
        )
        try:
            if not missing:
                # cache already covers the requested window -> skip network + rewrite;
                # the cached days' partitions already exist on disk.
                return ({
                    "symbol": symbol,
                    "ts_code": ts_code,
                    "status": "ok",
                    "source": "cache",
                    "history_source": "cache",
                    "raw_source": "cache",
                    "elapsed_seconds": 0.0,
                    "elapsed_total_seconds": 0.0,
                    "elapsed_detail_seconds": {"raw": 0.0, "qfq": 0.0},
                    "ohlc_backfilled": False,
                    "sessions_derived": 0,
                    "sessions_without_adjusted_close": 0,
                    "cached": True,
                }, [])
            # Include one observed boundary as an anchor. An exact one-day query may
            # legitimately return no row when the stock is suspended; the anchor
            # distinguishes a healthy endpoint with no new K-line from an entirely
            # empty/failed response, without inventing a bar for the missing day.
            fetch_first = min(filter(None, [min(missing), observed_last]))
            fetch_last = max(filter(None, [max(missing), observed_first]))
            mstart = fetch_first.replace("-", "")
            mend = fetch_last.replace("-", "")
            frame, history_source, _attempts, elapsed = fetch_qfq_frame(
                ak, stock, mstart, mend, retries, min_interval, primary_source=primary,
            )
            fetched = frame_ohlc_rows(frame, ts_code, set(sessions))
            if not fetched and not qfq_by_date:
                raise ValueError("qfq frame contains no requested sessions")
            for row in fetched:
                day = row["trade_date"]
                qfq_by_date[day] = {
                    "trade_date": day,
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                }
            # is_limit_up 由 K 线按官方涨跌幅限制本地计算（每轮确定性重算，非网络源）；
            # v5 无因子，直接用 qfq 收盘比值（除权日 qfq 已吸收公司行为）。
            annotate_limit_up(qfq_by_date, ts_code)
            store_payload = prices_v4.build_store(
                ts_code,
                stock.get("list_date"),
                list(qfq_by_date.values()),
                f"akshare_{history_source}",
                now_cst().isoformat(timespec="seconds"),
                min(filter(None, [store.get("requested_first"), sessions[0]])),
                max(filter(None, [store.get("requested_last"), sessions[-1]])),
            )
            refresh_limit_up_in_payload(store_payload, ts_code)
            prices_v4.write_store(output_dir, store_payload)
            derived = prices_v4.derive_rows(store_payload)
            missing_set = set(missing)
            selected_rows = [
                row for row in derived["hfq"]
                if row["trade_date"] in missing_set and row["close"] is not None
            ]
            dropped = len(missing) - len(selected_rows)
            return ({
                "symbol": symbol,
                "ts_code": ts_code,
                "status": "ok",
                "source": "network",
                "history_source": history_source,
                "raw_source": history_source,
                "elapsed_seconds": round(elapsed, 3),
                "elapsed_total_seconds": round(elapsed, 3),
                "elapsed_detail_seconds": {"raw": 0.0, "qfq": round(elapsed, 3)},
                "ohlc_backfilled": True,
                "factor_events": 0,
                "sessions_derived": len(selected_rows),
                "sessions_without_adjusted_close": dropped,
            }, selected_rows)
        except Exception as exc:  # noqa: BLE001 - isolate one stock failure
            return ({
                "symbol": symbol,
                "ts_code": ts_code,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }, [])



    # 任务队列：每只股一个 _FetchTask。worker 池并发取任务；失败则 attempt+1、记录原因、
    # 重新入队（最多 MAX_TASK_ATTEMPTS 次任务级尝试）；全部失败进入 dead_letter 并保留历次原因。
    # 用 outstanding 计数（尚未彻底解决的个股数）驱动 worker 退出：重投不改变计数，成功/死信各减 1；
    # 队列空且 outstanding==0 时所有 worker 退出，避免哨兵被重投任务挤到后方导致提前结束。
    task_q: "Queue[_FetchTask]" = Queue()
    for stock in universe:
        task_q.put(_FetchTask(stock, 0, [], []))

    manifest: list[dict[str, Any]] = []
    dead_letter: list[dict[str, Any]] = []
    partitions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lock = threading.Lock()
    resolved = {"n": 0}
    outstanding = {"n": len(universe)}

    def worker() -> None:
        while True:
            try:
                task = task_q.get_nowait()
            except Empty:
                with lock:
                    if outstanding["n"] == 0:
                        break
                time.sleep(0.01)
                continue
            stock = task.stock
            ts_code = stock["ts_code"]
            attempt_no = task.attempt + 1  # 1 起的任务级尝试序号
            try:
                primary = pick_retry_source(
                    stock, enabled, weights, list(task.tried_sources),
                )
                request, rows = fetch_one(stock, primary)
            except Exception as exc:  # noqa: BLE001 - 任何失败都视为可重投的任务失败
                request = {
                    "symbol": stock["symbol"],
                    "ts_code": ts_code,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                rows = []
                primary = None
            tried = list(task.tried_sources) + ([primary] if primary else [])
            if request["status"] == "failed":
                reasons = list(task.reasons) + [request["error"]]
            else:
                reasons = list(task.reasons)
            request["attempts"] = attempt_no
            request["tried_sources"] = tried
            request["reasons"] = reasons
            with lock:
                if request["status"] == "ok":
                    resolved["n"] += 1
                    outstanding["n"] -= 1
                elif attempt_no < max_attempts:
                    # 失败重投：保留已尝试次数与历次原因，下次可能换源（换源时双腿同源重抓）
                    task_q.put(_FetchTask(stock, attempt_no, tried, reasons))
                else:
                    dead_letter.append(request)
                    resolved["n"] += 1
                    outstanding["n"] -= 1
                request["progress"] = f"{resolved['n']}/{len(universe)}"
                if request["status"] == "ok":
                    manifest.append(request)
                    for row in rows:
                        partitions[row["trade_date"]].append(row)

    threads = [
        threading.Thread(target=worker, name=f"fetch-w{idx}")
        for idx in range(max(1, workers))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if partitions_out is None:
        for trade_date, rows in partitions.items():
            merge_partition(daily_dir / f"{trade_date}.json", rows)
    else:
        # 进程分片：分片只写自己的日分区切片，由父进程统一合并。若让 N 个进程同时对
        # 同一个 daily_adjusted/<date>.json 做 read-modify-write，后写者会盖掉先写者
        # 的行（merge_partition 是整文件重写，不是追加）。
        partitions_out.mkdir(parents=True, exist_ok=True)
        for trade_date, rows in partitions.items():
            atomic_write_json(partitions_out / f"{trade_date}.json", rows)
    return manifest, dead_letter


def completed_sessions(calendar: list[str], end_date: str, required: int) -> list[str]:
    eligible = [item for item in calendar if item <= end_date]
    if len(eligible) < required:
        raise ValueError(f"calendar has {len(eligible)} sessions through {end_date}; need {required}")
    return eligible[-required:]


def read_partition_map(path: Path) -> dict[str, dict[str, Any]]:
    return {row["ts_code"]: row for row in read_json_rows(path) if row.get("ts_code")}


def _snapshot_window(calendar: list[str], now: "datetime | None" = None) -> "tuple[str | None, str]":
    """Return (latest observable session, threshold date iso) for a spot snapshot.

    Beijing time 15:05 marks the close: at/after that the current session is the
    latest observable; before that (intraday / pre-open) the latest observable is
    the previous trading day. This fixes the old bug where, in the small hours,
    ``date.today()`` already advanced to the new session and snapshotting the
    previous session was wrongly rejected.
    """
    now = now or now_cst()
    if now.hour > 15 or (now.hour == 15 and now.minute >= 5):
        threshold = now.date()
    else:
        threshold = (now - timedelta(days=1)).date()
    threshold_iso = threshold.isoformat()
    candidates = [item for item in calendar if item <= threshold_iso]
    return (candidates[-1] if candidates else None), threshold_iso




def limit_ratio_for_ts_code(ts_code: str) -> float:
    """Official daily price-limit ratio by board, derived from the ts_code.

    Main board (沪市主板/深市主板) 10%, 科创板/创业板 20%, 北交所 30%.
    ST/*ST 5% is NOT applied here -- reliable historical ST status is not
    available in this workflow, so the board ratio is used throughout.
    """
    code = ts_code.split(".")[0]
    exch = ts_code.split(".")[-1].upper()
    if exch == "BJ":
        return 0.30
    if exch == "SH":
        if code.startswith(("688", "689")):
            return 0.20  # 科创板
        return 0.10
    if exch == "SZ":
        if code.startswith("30"):
            return 0.20  # 创业板
        return 0.10
    return 0.10


def limit_up_price(prev_close: float, ratio: float) -> float:
    """Return the qfq-caliber limit price, rounded half-up to one cent.

    The project deliberately uses one forward-adjusted price basis throughout.
    The minimum-tick fallback applies only when the rounded proportional limit
    differs from the previous close by less than one cent; it does *not* apply to
    every qfq price below one yuan.  The old ``prev < 1`` shortcut materially
    understated the limit for low qfq prices and created false positives.
    """
    tick = Decimal("0.01")
    prev = Decimal(str(prev_close)).quantize(tick, rounding=ROUND_HALF_UP)
    candidate = (prev * (Decimal("1") + Decimal(str(ratio)))).quantize(
        tick, rounding=ROUND_HALF_UP
    )
    if abs(candidate - prev) < tick:
        candidate = prev + tick
    return float(candidate)


def compute_limit_up(prev_close: float, close: float, ratio: float) -> bool:
    """True when `close` reaches the official limit-up price (within rounding).

    The intraday-high check is omitted: high >= close always holds, so a close at
    the limit price already implies the limit was touched.
    """
    if prev_close is None or close is None or prev_close <= 0 or close <= 0:
        return False
    limit_price = limit_up_price(prev_close, ratio)
    # AkShare's Sina and Tencent qfq series can differ by one cent on the
    # predecessor close while agreeing on the current close. Treat one source
    # tick plus the half-cent comparison tolerance as the same limit-up event;
    # otherwise a weighted source choice can flip a borderline board between
    # identical cold starts (for example 688001.SH on 2026-09-07).
    return close >= limit_price - 0.015


def annotate_limit_up(
    raw_by_date: "dict[str, dict]",
    ts_code: str,
    factor_by_date: "dict[str, dict] | None" = None,
    is_st: bool = False,
) -> None:
    """Compute is_limit_up in place for every row in raw_by_date from its K-line.

    Caliber — forward-adjusted (前复权). The limit-up test is a one-day ratio, so
    evaluating it on the adjusted series (raw close × hfq factor) is numerically
    identical to the qfq basis: the qfq anchor factor cancels in the ratio. It
    diverges from the raw (unadjusted) basis ONLY on ex-dividend / ex-right days,
    where the adjusted series correctly absorbs the corporate action (a dividend
    drop is not a limit-up). This makes the limit-up caliber uniform with the
    return / height metrics, which are all computed on the qfq series.

    prev_close is the adjusted close of the immediately preceding session present
    in raw_by_date; a session without a predecessor (or with a non-positive /
    missing adjusted close, or a missing factor) gets is_limit_up = None
    (data_insufficient). Recomputed deterministically on every run, so it is a
    derived field, not a network source.

    When factor_by_date is omitted the raw close is used as a backward-compatible
    fallback; production fetch paths always pass factors so the caliber is
    uniformly forward-adjusted.
    """
    ratio = 0.05 if is_st else limit_ratio_for_ts_code(ts_code)

    def adj_close_of(day: str) -> "float | None":
        row = raw_by_date.get(day)
        if not isinstance(row, dict):
            return None
        close = prices_v4.as_float(row.get("close"))
        if close is None or close <= 0:
            return None
        if factor_by_date:
            frow = factor_by_date.get(day)
            factor = prices_v4.as_float(frow.get("factor")) if isinstance(frow, dict) else None
            if factor is None or factor <= 0:
                return None
            close = close * factor
        return close

    ordered = sorted(raw_by_date)
    prev_close = None
    for day in ordered:
        close = adj_close_of(day)
        if prev_close is not None and close is not None:
            raw_by_date[day]["is_limit_up"] = compute_limit_up(prev_close, close, ratio)
        else:
            raw_by_date[day]["is_limit_up"] = None
        if close is not None:
            prev_close = close






def refresh_limit_up_in_payload(payload: dict[str, Any], ts_code: str) -> int:
    """Recompute ``is_limit_up`` on the *finalized* store payload, in place.

    The first annotation inside a fetch attempt runs on in-memory dicts. If a
    session's factor is missing at that moment, ``annotate_limit_up`` falls back to
    an earlier predecessor for ``prev_close``, which silently turns a borderline
    limit-up into False. ``build_store`` then normalises the payload, so the
    persisted rows can disagree with a recompute of the same payload -- exactly
    what ``validate_cache_contract.py`` flags as ``limit_up_field_inconsistent``
    (measured: 19 rows in the 2026-10-26 single-process build, 32 in the re-run,
    all SZ/SH 10% boards sitting ~1 tick from the limit).

    ``is_limit_up`` is a derived field, so the only correct precedent is the data
    actually being written. Returns the number of rows whose mark changed.
    """
    # v5: the store carries a single qfq series; the factor/adjust machinery is
    # gone, so is_limit_up is recomputed directly from the qfq close ratio.
    qfq_rows = {row["trade_date"]: row for row in payload.get("qfq") or [] if isinstance(row, dict) and row.get("trade_date")}
    if not qfq_rows:
        return 0
    before = {day: row.get("is_limit_up") for day, row in qfq_rows.items()}
    annotate_limit_up(qfq_rows, ts_code)
    return sum(1 for day, row in qfq_rows.items() if row.get("is_limit_up") != before.get(day))


def live_session_of(sessions: list[str]) -> "str | None":
    """The latest session that is on or before today — the real-time session.

    Data for this session may still be updating (late prints, provisional
    factors), so callers use it to decide whether to re-fetch its raw prices and
    adjustment factor. Note: is_limit_up is now a K-line-derived field recomputed
    every run, so the live session needs no special limit-up refresh.
    """
    today = date_today_cst().isoformat()
    eligible = [item for item in sessions if item <= today]
    return max(eligible) if eligible else None





def cmd_status(output_dir: Path, as_of: str) -> dict[str, Any]:
    """只读水位探测：汇总 calendar / price_store 的覆盖情况。

    不触发任何网络请求，也不写缓存。is_limit_up 现已由 K 线按官方涨跌幅限制本地
    计算（派生字段，无网络/保留窗口依赖），故不再单独探测涨停池覆盖。返回
    delta_sessions、gap_sessions 与建议的 next_action，供调用方决定下一步走
    snapshot / bootstrap / backfill。
    """
    cal_path = output_dir / "calendar.json"
    if not cal_path.exists():
        return {"ok": False, "error": "calendar.json not found; run bootstrap first", "next_action": "bootstrap"}
    calendar = sorted({
        normalize_date(row.get("cal_date") or row.get("trade_date"))
        for row in read_json_rows(cal_path)
    })
    if not calendar:
        return {"ok": False, "error": "calendar.json is empty", "next_action": "bootstrap"}

    # 汇总 price_store（权威层）覆盖的会话集合
    store_dir = output_dir / prices_v4.STORE_DIR
    covered: set[str] = set()
    if store_dir.is_dir():
        for path in store_dir.glob("*.json"):
            try:
                store = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for row in store.get("qfq") or []:
                day = row.get("trade_date")
                if day:
                    covered.add(day)
    covered_sessions = sorted(s for s in covered if s in set(calendar))

    # is_limit_up 为派生字段，无保留窗口；无需探测涨停池覆盖。

    last_calendar = calendar[-1]
    today_cst = date_today_cst().isoformat()
    last_covered = covered_sessions[-1] if covered_sessions else None
    if last_covered is not None:
        delta = len(calendar) - 1 - calendar.index(last_covered)
        # 真正需要补的缺口 = 最后覆盖日之后的交易日。本缓存是滑动窗口（只保留最近 N 日），
        # 历史日本就不在 scope，不应计入"缺失"，否则会把整段 A 股历史误报成缺口。
        gap_sessions = [s for s in calendar if last_covered < s <= today_cst]
    else:
        delta = None
        gap_sessions = []

    if last_covered is None:
        next_action = "bootstrap"
    elif last_covered == last_calendar:
        next_action = "idle"
    elif delta == 1 and last_covered == calendar[-2]:
        next_action = "bootstrap"
    elif isinstance(delta, int) and delta >= 2:
        next_action = "bootstrap_or_backfill"
    else:
        next_action = "bootstrap"

    calendar_behind = last_calendar < today_cst
    note = None
    if delta == 1:
        note = "bootstrap 追加最新会话：北京时间 15:05 前 latest_observable 为上一交易日，可补昨日"
    if calendar_behind:
        behind_note = (
            f"缓存日历末日 {last_calendar} 落后于真实今天 {today_cst}；"
            "先 --refresh-calendar 再跑 status 才能看到真实缺口"
        )
        note = f"{note}; {behind_note}" if note else behind_note

    return {
        "ok": True,
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "as_of": as_of,
        "today_cst": today_cst,
        "calendar_last": last_calendar,
        "calendar_session_count": len(calendar),
        "calendar_behind_real_today": calendar_behind,
        "price_sessions_covered": len(covered_sessions),
        "last_covered_session": last_covered,
        "delta_sessions": delta,
        "gap_session_count": len(gap_sessions),
        "gap_sessions_preview": gap_sessions[:10],
        "next_action": next_action,
        "note": note,
    }


def split_universe(
    master: list[dict[str, Any]],
    selected_symbols: set[str],
    shard_count: int,
    shard_index: int,
) -> list[dict[str, Any]]:
    """Return one contiguous slice of the deterministic universe.

    Contiguous (not round-robin) because ``master`` is already sorted by
    ``ts_code``: each shard then owns a code range, so merged partitions stay
    sorted and no extra ordering pass is needed. ``shard_count <= 1`` returns the
    whole universe, which keeps the single-process path byte-identical.
    """
    universe = [row for row in master if not selected_symbols or row["symbol"] in selected_symbols]
    if shard_count <= 1:
        return universe
    per_shard = (len(universe) + shard_count - 1) // shard_count
    start = shard_index * per_shard
    return universe[start:start + per_shard]


def summarize_sources(manifest: list[dict[str, Any]], failures: list[dict[str, Any]]) -> dict[str, Any]:
    """Tally vendor calls / failures / latency **per source**, by attempt.

    Why by attempt and not by final source: ``tried_sources`` records every
    source-pinned try in order, so a flaky vendor can absorb 30% of the attempts
    while owning almost none of the successes. Measured on the 2026-10-26
    single-process run, eastmoney took 30.0% of attempts, failed 47.2% of them,
    and still shows up as only 10.5% of the successful ``history_source`` values.
    Looking only at final sources therefore hides the failure rate entirely.
    """
    attempts: dict[str, int] = defaultdict(int)
    succeeded: dict[str, int] = defaultdict(int)
    latency: dict[str, list[float]] = defaultdict(list)
    for row in list(manifest) + list(failures):
        tried = [str(item) for item in (row.get("tried_sources") or []) if item]
        if not tried:
            # 早期记录没有 tried_sources：退化为终态源，只能归给最后那个源
            final = str(row.get("history_source") or row.get("source") or "unknown").replace("_hfq", "").replace("_raw", "")
            tried = [final]
        for source in tried:
            attempts[source] += 1
        if row.get("status") == "ok":
            succeeded[tried[-1]] += 1
            elapsed = row.get("elapsed_total_seconds") or row.get("elapsed_seconds")
            if elapsed:
                latency[tried[-1]].append(float(elapsed))
    total_attempts = sum(attempts.values())
    by_source: dict[str, Any] = {}
    for source, count in sorted(attempts.items(), key=lambda item: -item[1]):
        ok = succeeded.get(source, 0)
        failed = count - ok
        samples = sorted(latency.get(source) or [])
        by_source[source] = {
            "attempts": count,
            "attempt_share": round(count / total_attempts, 4) if total_attempts else None,
            "succeeded": ok,
            "failed": failed,
            "failure_rate": round(failed / count, 4) if count else None,
            "latency_p50": round(samples[len(samples) // 2], 3) if samples else None,
            "latency_mean": round(sum(samples) / len(samples), 3) if samples else None,
            "latency_p95": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 3) if samples else None,
            "latency_samples": len(samples),
        }
    return {
        "unit": "attempt = one source-pinned try at one symbol (retries counted separately)",
        "total_attempts": total_attempts,
        "symbols_ok": sum(item.get("status") == "ok" for item in manifest),
        "symbols_failed": len(failures),
        "by_source": by_source,
    }


def build_bootstrap_summary(
    *,
    ak: Any,
    args: "argparse.Namespace",
    generated_at: str,
    as_of: str,
    calendar_upstream_last: "str | None",
    sessions: list[str],
    master: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    enabled: dict[str, bool],
    elapsed_seconds: "float | None" = None,
    shard_report: "list[dict[str, Any]] | None" = None,
    shared_v8: bool = False,
) -> dict[str, Any]:
    """Single source of truth for the bootstrap summary payload.

    Both the single-process path and the process-sharded parent build their
    summary here, so the audit contract cannot drift between the two modes.
    """
    summary: dict[str, Any] = {
        "generated_at": generated_at,
        "mode": "bootstrap",
        "schema_version": SCHEMA_VERSION,
        "akshare_version": getattr(ak, "__version__", "unknown"),
        "basis": "qfq_direct",
        "source_enabled": enabled,
        "as_of": as_of,
        "calendar_upstream_last": calendar_upstream_last,
        "sessions": sessions,
        "coverage": {"first_session": sessions[0], "last_session": sessions[-1], "session_count": len(sessions)},
        "request": {
            "target_count": args.target_count,
            "gain_lookback_sessions": args.lookback,
            "buffer_sessions": args.buffer_sessions,
            "required_session_count": len(sessions),
        },
        "price_store_dir": str(args.output_dir / prices_v4.STORE_DIR),
        "daily_adjusted_dir": str(args.output_dir / "daily_adjusted"),
        "limit_up_source": "kline_official_ratio (derived from qfq close vs prior close, official board ratio)",
        "authority_layers": [prices_v4.STORE_DIR, "calendar", "stock_basic"],
        "derived_note": "v5 stores a single qfq series; raw/hfq/qfq/view are the same qfq projection. run derive_cache.py --mode derive for forward_daily/unadjusted_daily",
        "sessions_without_adjusted_close": sum(item.get("sessions_without_adjusted_close", 0) for item in manifest),
        "universe_size": len(master),
        "requested_symbols": args.symbols,
        "successful_symbols": sum(item["status"] == "ok" for item in manifest),
        "failed_symbols": len(failures),
        "failures": failures,
        "historical_st_status": "not_available_from_this_adapter",
        "parallel_mode": "process_shards" if shard_report else "single_process",
        "v8_shared_context": shared_v8,
        "workers_per_process": args.workers,
        "shard_count": len(shard_report) if shard_report else 1,
        "elapsed_seconds": elapsed_seconds,
        "source_stats": summarize_sources(manifest, failures),
    }
    if shard_report:
        summary["shards"] = shard_report
    return summary


def merge_shard_partitions(
    output_dir: Path,
    shard_dirs: list[Path],
    sessions: "list[str] | None" = None,
) -> dict[str, int]:
    """Merge shard slices one date at a time to bound memory and ignore stale runs."""
    if sessions is None:
        trade_dates = sorted({
            path.stem
            for shard_dir in shard_dirs if shard_dir.exists()
            for path in shard_dir.glob("*.json")
        })
    else:
        trade_dates = sorted(set(sessions))
    daily_dir = output_dir / "daily_adjusted"
    rows_per_date: dict[str, int] = {}
    for trade_date in trade_dates:
        merged: dict[str, dict[str, Any]] = {}
        for shard_dir in shard_dirs:
            path = shard_dir / f"{trade_date}.json"
            for row in read_json_rows(path):
                if row.get("ts_code"):
                    merged[row["ts_code"]] = row
        if not merged:
            continue
        rows = list(merged.values())
        merge_partition(daily_dir / f"{trade_date}.json", rows)
        rows_per_date[trade_date] = len(rows)
    return rows_per_date


def run_sharded_bootstrap(
    ak: Any,
    args: "argparse.Namespace",
    master: list[dict[str, Any]],
    sessions: list[str],
    enabled: dict[str, bool],
    weights: dict[str, float],
    generated_at: str,
    as_of: str,
    calendar_upstream_last: "str | None",
    shared_v8: bool = False,
) -> dict[str, Any]:
    """Fan the bootstrap out over N OS processes, each running its own thread pool.

    Why processes: per-symbol CPU (~0.5s, dominated by AkShare's internal
    py_mini_racer JS decode of the full history) is GIL-bound, so extra threads in
    one process stop paying off well before the machine's cores are used. Each
    shard keeps the existing single-process worker pool, so the per-symbol logic
    (source pinning, two-leg factor basis, retry/dead-letter) is untouched.
    """
    shard_count = args.shards
    base_dir = args.output_dir / "_shards"
    base_dir.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    common = [
        sys.executable, script,
        "--mode", "bootstrap",
        "--output-dir", str(args.output_dir),
        "--end-date", sessions[-1],
        "--target-count", str(args.target_count),
        "--lookback", str(args.lookback),
        "--buffer-sessions", str(args.buffer_sessions),
        "--workers", str(args.workers),
        "--retries", str(args.retries),
        "--task-attempts", str(args.task_attempts),
        "--min-interval", str(args.min_interval),
        "--rate-limit", str(args.rate_limit),
        "--as-of", args.as_of or as_of,
        "--shards", str(shard_count),
    ]
    if args.source_rates:
        common += ["--source-rates", str(args.source_rates)]
    if args.source_weights:
        common += ["--source-weights", str(args.source_weights)]
    if args.symbols:
        common += ["--symbols", *args.symbols]

    started = time.time()
    shard_state: dict[int, dict[str, Any]] = {idx: {"index": idx, "attempts": []} for idx in range(shard_count)}
    wave = [(idx, 0) for idx in range(shard_count)]
    while wave:
        running = []
        for idx, attempt in wave:
            log_path = base_dir / f"shard{idx}.log"
            # 子进程的 tqdm / 逐股日志必须落文件：8 个进程共用终端会把进度条搅成乱码
            handle = log_path.open("a" if attempt else "w", encoding="utf-8")
            proc = subprocess.Popen(
                common + ["--shard-index", str(idx)],
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
            running.append({"index": idx, "attempt": attempt, "proc": proc, "log": handle, "start": time.time()})
        retry_wave: list[tuple[int, int]] = []
        for item in running:
            item["proc"].wait()
            elapsed = time.time() - item["start"]
            item["log"].close()
            code = item["proc"].returncode
            shard_state[item["index"]]["attempts"].append({
                "attempt": item["attempt"] + 1,
                "returncode": code,
                "elapsed_seconds": round(elapsed, 2),
            })
            # 退出码 0/2 表示"跑完了"（2 只是有死信）；其它值基本是原生 abort（V8/内存），
            # 这种崩溃 Python 抓不住，只能由父进程重启。重跑是安全且廉价的：已落盘的股票
            # 跳过网络，分区行由 price_store 重建，不会丢任何一个已成功的会话。
            if code not in (0, 2) and item["attempt"] < args.shard_retries:
                retry_wave.append((item["index"], item["attempt"] + 1))
        wave = retry_wave

    shard_report: list[dict[str, Any]] = []
    for idx in range(shard_count):
        shard_dir = base_dir / f"part{idx}"
        manifest_path = shard_dir / "manifest.json"
        shard_manifest: dict[str, Any] = {}
        if manifest_path.exists():
            shard_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        attempts = shard_state[idx]["attempts"]
        last_code = attempts[-1]["returncode"] if attempts else None
        shard_report.append({
            "index": idx,
            "returncode": last_code,
            "attempts": attempts,
            "restarted": len(attempts) > 1,
            "elapsed_seconds": round(sum(item["elapsed_seconds"] for item in attempts), 2),
            "symbols": shard_manifest.get("symbols", 0),
            "successful_symbols": shard_manifest.get("successful_symbols", 0),
            "failed_symbols": shard_manifest.get("failed_symbols", 0),
            "complete": bool(manifest_path.exists() and last_code in (0, 2)),
            "workers": args.workers,
            "log": str(base_dir / f"shard{idx}.log"),
        })

    manifest: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for item in shard_report:
        shard_dir = base_dir / f"part{item['index']}"
        manifest_path = shard_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.extend(payload.get("requests") or [])
        failures.extend(payload.get("failures") or [])
    manifest.sort(key=lambda row: str(row.get("ts_code") or ""))

    rows_per_date = merge_shard_partitions(
        args.output_dir,
        [base_dir / f"part{item['index']}" / "daily" for item in shard_report],
        sessions,
    )
    elapsed = time.time() - started
    summary = build_bootstrap_summary(
        ak=ak,
        args=args,
        generated_at=generated_at,
        as_of=as_of,
        calendar_upstream_last=calendar_upstream_last,
        sessions=sessions,
        master=master,
        manifest=manifest,
        failures=failures,
        enabled=enabled,
        elapsed_seconds=round(elapsed, 2),
        shard_report=shard_report,
        shared_v8=shared_v8,
    )
    summary["merged_partitions"] = {"dates": len(rows_per_date), "rows": sum(rows_per_date.values())}
    summary["throughput_symbols_per_second"] = (
        round(len(manifest) / elapsed, 3) if elapsed > 0 and manifest else None
    )
    atomic_write_json(args.output_dir / "bootstrap_manifest.json", {"summary": summary, "requests": manifest})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and cache A-share data through AkShare")
    parser.add_argument("--mode", choices=("bootstrap", "status"), required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--end-date", required=True, help="completed trading session, YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--start-date", default=None,
                        help="DEPRECATED and IGNORED: the window is derived from cache content plus "
                             "--target-count/--lookback/--buffer-sessions. Kept only so old command "
                             "lines do not hard-fail; passing it prints a warning")
    parser.add_argument("--target-count", type=int, default=30)
    parser.add_argument("--lookback", type=int, default=10)
    parser.add_argument(
        "--buffer-sessions",
        type=int,
        default=30,
        help="在目标交易日和收益计算回看期之外，额外向前缓存的交易日数（默认30）",
    )
    parser.add_argument("--min-interval", type=float, default=0.0,
                        help="optional fixed floor (seconds) added to the exponential retry backoff between "
                             "attempts; default 0 means no artificial spacing (rely on source-side limits + retry)")
    parser.add_argument("--rate-limit", type=float, default=1.0,
                        help="kill switch only: 0 disables EVERY source, any value >0 keeps the default "
                             "roster (sina/tencent on, eastmoney off). The global request-rate limiter was "
                             "removed, so this no longer caps request rate and no longer blanket-enables "
                             "sources -- use --source-rates to switch an individual source")
    parser.add_argument("--source-rates", type=Path, default=None,
                        help="JSON map overriding the source roster, e.g. {\"eastmoney\": true} re-enables "
                             "eastmoney; {\"tencent\": false} disables tencent. Keys absent from the file keep "
                             "their default (sina on, eastmoney off, tencent on). A disabled source is never "
                             "assigned work")
    parser.add_argument("--source-weights", type=Path, default=None,
                        help="JSON map of per-source fetch weights, e.g. {\"sina\": 5, \"eastmoney\": 3, "
                            "\"tencent\": 2}; higher weight = proportionally more tasks. Orthogonal to "
                            "--source-rates (a disabled source gets no work regardless of weight)")
    parser.add_argument("--retries", type=int, default=1,
                        help="HTTP retries INSIDE one attempt (default 1 = no retry). Retrying the same "
                             "source is mostly wasted work: eastmoney fails 47%%~99%% of calls with "
                             "RemoteDisconnected, and the task-level retry re-picks a source anyway. Set >1 "
                             "only for a source you trust to be flaky-but-recoverable")
    parser.add_argument("--task-attempts", type=int, default=3,
                        help="task-level attempts per symbol (default 3). Each attempt re-picks a source and "
                             "re-fetches both legs from it. 1 = no task retry at all (any transient failure "
                             "becomes a dead letter)")
    parser.add_argument("--workers", type=int, default=32,
                        help="concurrency (default 32, max %d); more workers overlap per-call network "
                             "latency across the three source pipelines; once a source is the latency "
                             "ceiling, extra workers add no throughput" % WORKER_LIMIT)
    parser.add_argument("--symbols", nargs="*", default=[], help="optional six-digit subset for smoke tests")
    parser.add_argument("--as-of", default=None,
                        help="observation date (YYYY-MM-DD/YYYYMMDD); the persisted calendar is truncated here. "
                             "Defaults to today. Pass an earlier date to replay a past window deterministically")
    parser.add_argument("--refresh-calendar", action="store_true")
    parser.add_argument("--refresh-master", action="store_true")
    parser.add_argument("--confirm-completed-session", action="store_true")
    parser.add_argument("--shards", type=int, default=1,
                        help="bootstrap only: split the universe across N OS processes, each running its own "
                             "--workers thread pool. Needed because per-symbol CPU (AkShare's internal JS "
                             "decode) is GIL-bound, so extra threads in one process stop scaling long before "
                             "the machine's cores are used. 1 (default) keeps the single-process behaviour "
                             "byte-identical to earlier versions; 6-10 is a sane range for a 16-core box")
    parser.add_argument("--shard-index", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--shard-retries", type=int, default=2,
                        help="bootstrap only: how many times the parent may relaunch a shard process "
                             "that exits abnormally (a native V8 abort, not a Python exception). "
                             "Relaunch is safe and cheap: symbols already written to price_store skip the "
                             "network and their partition rows are rebuilt from the store")
    args = parser.parse_args()

    if args.target_count <= 0 or args.lookback <= 0 or args.buffer_sessions < 0:
        parser.error("target-count/lookback must be positive; buffer-sessions cannot be negative")
    if args.min_interval < 0 or args.retries <= 0:
        parser.error("min-interval cannot be negative and retries must be positive (1 = no retry)")
    if args.task_attempts < 1:
        parser.error("--task-attempts must be >= 1 (1 = no task-level retry)")
    if args.rate_limit < 0:
        parser.error("--rate-limit must be >= 0")
    if args.workers <= 0 or args.workers > WORKER_LIMIT:
        parser.error(f"workers must be between 1 and {WORKER_LIMIT}")
    if args.shards < 1 or args.shards > SHARD_LIMIT:
        parser.error(f"shards must be between 1 and {SHARD_LIMIT}")
    if args.shards > 1 and args.mode != "bootstrap":
        parser.error("--shards is only supported in --mode bootstrap")
    if args.shard_index is not None and not (0 <= args.shard_index < args.shards):
        parser.error("--shard-index must satisfy 0 <= index < shards")
    if args.start_date:
        print("[warn] --start-date 已废弃且不生效：抓取窗口由缓存内容与 "
              "--target-count/--lookback/--buffer-sessions 共同决定；如需更早历史请加大 buffer-sessions。",
              file=sys.stderr)

    if args.mode == "status":
        as_of = normalize_date(args.as_of) if args.as_of else date_today_cst().isoformat()
        report = cmd_status(args.output_dir, as_of)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    ak, _ = import_dependencies()
    # 必须在起任何工作线程之前装好：并发构造 MiniRacer 会让 V8 的 partition allocator
    # 二次初始化并 abort 整个进程（见 install_shared_v8 的注释）。
    shared_v8 = install_shared_v8()
    end_date = normalize_date(args.end_date)
    as_of = normalize_date(args.as_of) if args.as_of else date_today_cst().isoformat()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    calendar, calendar_upstream_last = fetch_calendar(ak, args.output_dir, args.refresh_calendar, as_of)
    if not calendar:
        raise ValueError(f"AkShare calendar has no session on or before as_of={as_of}")
    if end_date > as_of:
        # 上游预排表里可能有这一天，但它还没发生/还没收盘，不能当已完成会话用
        horizon = f"（上游预排表末尾 {calendar_upstream_last}）" if calendar_upstream_last else ""
        raise ValueError(f"--end-date {end_date} 晚于观测日 as_of={as_of}{horizon}；未来日期没有可交付的日K")
    # end_date 落在周末/节假日不报错：取它之前最近的已收盘会话即可，
    # 具体够不够由 completed_sessions() 按 target+lookback+buffer 判定
    master: list[dict[str, Any]] = []
    if args.mode == "bootstrap":
        master = fetch_master(ak, args.output_dir, args.refresh_master)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    enabled = resolve_source_rates(args)
    weights = resolve_source_weights(args)

    if args.mode == "bootstrap":
        # One extra predecessor session is required to derive is_limit_up for the
        # first analytical/cache session.  Without it every symbol's first row is
        # necessarily null, and the T3 first-board search can incorrectly downgrade
        # otherwise complete candidates to data_insufficient.
        required = args.target_count + args.lookback + args.buffer_sessions + 1
        sessions = completed_sessions(calendar, end_date, required)
        # 父进程：把股票池切成 N 个分片，交给 N 个 OS 进程各跑一个线程池。
        # 只有父进程写最终 manifest / daily_adjusted，子进程只写自己的切片。
        if args.shards > 1 and args.shard_index is None:
            summary = run_sharded_bootstrap(
                ak, args, master, sessions, enabled, weights, generated_at, as_of, calendar_upstream_last,
                shared_v8=shared_v8,
            )
            print(json.dumps(summary, ensure_ascii=False))
            return 0 if not summary.get("failures") else 2

        shard_dir: "Path | None" = None
        if args.shard_index is not None:
            shard_dir = args.output_dir / "_shards" / f"part{args.shard_index}"
            shard_dir.mkdir(parents=True, exist_ok=True)
        universe = split_universe(master, {symbol6(item) for item in args.symbols}, args.shards, args.shard_index or 0)
        shard_started = time.time()
        manifest, failures = bootstrap(
            ak,
            args.output_dir,
            universe,
            sessions,
            args.retries,
            args.min_interval,
            set(),
            args.workers,
            enabled,
            weights,
            partitions_out=(shard_dir / "daily") if shard_dir else None,
            max_attempts=args.task_attempts,
        )
        elapsed = round(time.time() - shard_started, 2)
        if shard_dir is not None:
            atomic_write_json(shard_dir / "manifest.json", {
                "shard_index": args.shard_index,
                "shard_count": args.shards,
                "symbols": len(universe),
                "successful_symbols": sum(item["status"] == "ok" for item in manifest),
                "failed_symbols": len(failures),
                "elapsed_seconds": elapsed,
                "workers": args.workers,
                "requests": manifest,
                "failures": failures,
            })
            print(json.dumps({
                "shard_index": args.shard_index,
                "shard_count": args.shards,
                "symbols": len(universe),
                "successful_symbols": sum(item["status"] == "ok" for item in manifest),
                "failed_symbols": len(failures),
                "elapsed_seconds": elapsed,
            }, ensure_ascii=False))
            return 0 if not failures else 2
        summary = build_bootstrap_summary(
            ak=ak,
            args=args,
            generated_at=generated_at,
            as_of=as_of,
            calendar_upstream_last=calendar_upstream_last,
            sessions=sessions,
            master=master,
            manifest=manifest,
            failures=failures,
            enabled=enabled,
            elapsed_seconds=elapsed,
            shared_v8=shared_v8,
        )
        atomic_write_json(args.output_dir / "bootstrap_manifest.json", {"summary": summary, "requests": manifest})
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if not failures else 2

    # --mode is constrained to {"bootstrap", "status"}; the status branch returns
    # early and the bootstrap branch returns above, so this is genuinely
    # unreachable. Kept as a guard so a future mode addition cannot silently fall
    # through without a summary.
    raise RuntimeError(f"unreachable mode: {args.mode}")


if __name__ == "__main__":
    exit_code = 0
    try:
        exit_code = main()
    except Exception as exc:  # noqa: BLE001 - CLI returns structured failure
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    # 共享 V8 上下文（install_shared_v8）会留下非守护线程，解释器正常退出会被卡住，
    # 父进程因此永远等不到子进程结束（2026-10-26 实测：8 个分片全部跑完却全部挂住）。
    # 这里已经刷过缓冲区，直接退出即可。
    os._exit(exit_code)
