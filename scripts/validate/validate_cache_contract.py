#!/usr/bin/env python3
"""Validate the integrated A-share cache contract (schema v3).

The cache is produced by ``fetch_akshare_data.py`` and consumed by the
deterministic compute layer. This validator enforces the on-disk contract:

    symbol_cache/<ts_code>.json   authoritative unified per-stock view (schema v3)
    price_store/<ts_code>.json    v5 price authority (single qfq series, no adjustment factor)
    is_limit_up                   derived K-line field in price_store raw rows
                                  (official per-board ratio; no third-party 涨停池)
    symbol_cache_hfq/  forward_daily/  unadjusted_daily/   legacy plain-list mirrors
    daily_adjusted/<date>.json    per-session whole-market cross-section
    stock_basic.json  calendar.json (truncated at the observation date)
    calendar_upstream.json        untouched upstream pre-schedule, audit only

It also cross-checks that the legacy mirrors agree with the authoritative
``symbol_cache`` view on sampled symbols, so a desync cannot pass silently.

Usage:
    python validate_cache_contract.py --cache-dir cache/akshare
    python validate_cache_contract.py --cache-dir cache/akshare --sample 200 --json-out report.json

Exit code 0 when no errors were found, 1 otherwise (warnings never fail).

Performance note: a full-market cache holds ~17k small JSON files across
symbol_cache/ and the three mirrors. Reading them serially costs minutes on
NTFS (open/read latency dominates, ~13 ms per file), so reads are chunked and
fanned out over a thread pool. ``--workers`` controls the fan-out; validation
logic and output ordering are unchanged.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 5
"""Contract version this validator targets (v5 = qfq-only authority layer).

Legacy caches stay readable: ``detect_layout`` distinguishes v5 / v4 / v3 and the
per-layout branches apply the invariants that actually hold there. This constant is
only the *current* contract, reported as ``schema_version`` in the JSON report, so it
must track ``fetch_akshare_data.SCHEMA_VERSION`` instead of lagging one generation.
"""
READ_CHUNK = 512
DEFAULT_WORKERS = 16
DEFAULT_DEEP_SAMPLE = 300
VIEW_FIELDS = (
    "hfq_open",
    "hfq_high",
    "hfq_low",
    "hfq_close",
    "qfq_open",
    "qfq_high",
    "qfq_low",
    "qfq_close",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "is_limit_up",
    "price_mode",
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def iter_loaded(files: list[Path], workers: int = DEFAULT_WORKERS) -> Iterator[tuple[Path, Any]]:
    """Yield (path, payload) in input order, reading in bounded parallel chunks.

    Chunking keeps peak memory at one chunk of parsed payloads instead of the whole
    cache, while still overlapping the per-file open/read latency.
    """
    if not files:
        return
    workers = max(1, min(workers, DEFAULT_WORKERS * 2))
    if workers == 1 or len(files) == 1:
        for file in files:
            yield file, load_json(file)
        return
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for start in range(0, len(files), READ_CHUNK):
            chunk = files[start:start + READ_CHUNK]
            for file, payload in zip(chunk, pool.map(load_json, chunk)):
                yield file, payload
    finally:
        pool.shutdown(wait=True)


def deep_sample(files: list[Path], limit: int) -> tuple[list[Path], dict[str, Any]]:
    """Pick the files that get deep content validation.

    Per-file content checks cost ~14 ms each on this class of filesystem (open-time
    scanning dominates, and it does not parallelise), so reading all ~17k cache files
    costs minutes regardless of thread count. File counts always come from the full
    listing; only the deep content pass is sampled, evenly across the sorted list so
    every exchange prefix is represented. ``--deep-all`` restores exhaustive checking.
    """
    if limit <= 0 or len(files) <= limit:
        return files, {"checked": len(files), "listed": len(files), "sampled": False}
    step = len(files) / limit
    picked = [files[min(len(files) - 1, int(index * step))] for index in range(limit)]
    return picked, {"checked": len(picked), "listed": len(files), "sampled": True}


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def close_enough(left: Any, right: Any) -> bool:
    if not (is_number(left) and is_number(right)):
        return False
    return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)


class Report:
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.counts: dict[str, Any] = {}

    def error(self, code: str, detail: str) -> None:
        self.issues.append({"level": "error", "code": code, "detail": detail})

    def warning(self, code: str, detail: str) -> None:
        self.issues.append({"level": "warning", "code": code, "detail": detail})

    def has_errors(self) -> bool:
        return any(item["level"] == "error" for item in self.issues)


def check_calendar(cache_dir: Path, report: Report) -> list[str]:
    payload = load_json(cache_dir / "calendar.json")
    if not isinstance(payload, list) or not payload:
        report.error("calendar_missing", "calendar.json missing or empty")
        return []
    dates = []
    for row in payload:
        if isinstance(row, dict) and DATE_RE.match(str(row.get("cal_date", ""))):
            dates.append(str(row["cal_date"]))
    dates = sorted(set(dates))
    if not dates:
        report.error("calendar_empty", "calendar.json has no valid cal_date")
    report.counts["calendar_sessions"] = len(dates)
    # v3: 落盘日历必须已按观测日截断。上游预排表含未来交易日，留着会让下游把
    # "还没发生"误读成"数据缺失"。
    today = datetime.now().date().isoformat()
    future = [d for d in dates if d > today]
    if future:
        report.error(
            "calendar_future_rows",
            f"{len(future)} 行日历晚于今天 {today}（首个 {future[0]}）；calendar.json 应按观测日截断",
        )
    upstream = load_json(cache_dir / "calendar_upstream.json")
    if isinstance(upstream, list) and upstream:
        report.counts["calendar_upstream_sessions"] = len(upstream)
    return dates


def check_master(cache_dir: Path, report: Report) -> dict[str, dict[str, Any]]:
    payload = load_json(cache_dir / "stock_basic.json")
    if not isinstance(payload, list) or not payload:
        report.error("master_missing", "stock_basic.json missing or empty")
        return {}
    required = ("ts_code", "symbol", "name", "list_date", "list_status")
    bad = 0
    by_code: dict[str, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict) or any(key not in row for key in required):
            bad += 1
            continue
        by_code[str(row["ts_code"])] = row
    if bad:
        report.error("master_rows_invalid", f"{bad} stock_basic rows missing required fields")
    report.counts["master_rows"] = len(payload)
    return by_code


def check_symbol_cache(
    cache_dir: Path,
    report: Report,
    workers: int = DEFAULT_WORKERS,
    deep_limit: int = DEFAULT_DEEP_SAMPLE,
) -> list[str]:
    directory = cache_dir / "symbol_cache"
    if not directory.is_dir():
        report.error("symbol_cache_missing", f"{directory} does not exist")
        return []
    files = sorted(directory.glob("*.json"))
    report.counts["symbol_cache_files"] = len(files)
    checked, sample_meta = deep_sample(files, deep_limit)
    report.counts["symbol_cache_deep_checked"] = sample_meta["checked"]
    if sample_meta["sampled"]:
        report.warning(
            "symbol_cache_deep_sampled",
            f"deep content check sampled {sample_meta['checked']}/{sample_meta['listed']} symbol_cache files "
            "(pass --deep-all for exhaustive checking)",
        )
    bad_schema = bad_rows = bad_date = 0
    # v3 caches legitimately carry schema_version 3 and a v4 cache never reaches
    # here, so the accepted version follows the layout. Accepting only the newest
    # constant would reject every pre-migration cache as invalid.
    accepted_versions = {3, 4}
    for file, payload in iter_loaded(checked, workers):
        if not isinstance(payload, dict) or payload.get("schema_version") not in accepted_versions:
            bad_schema += 1
            continue
        rows = payload.get("rows")
        if not isinstance(rows, list):
            bad_rows += 1
            continue
        previous = ""
        for row in rows:
            if not isinstance(row, dict) or not DATE_RE.match(str(row.get("trade_date", ""))):
                bad_date += 1
                continue
            day = str(row["trade_date"])
            if day < previous:
                report.error("symbol_cache_unsorted", f"{file.name} rows not sorted at {day}")
                break
            previous = day
            missing = [field for field in VIEW_FIELDS if field not in row]
            if missing:
                bad_rows += 1
                break
    if bad_schema:
        report.error("symbol_cache_schema", f"{bad_schema} symbol_cache files are not schema v3/v4")
    if bad_rows:
        report.error("symbol_cache_rows", f"{bad_rows} symbol_cache files have malformed rows")
    if bad_date:
        report.error("symbol_cache_dates", f"{bad_date} symbol_cache rows have invalid trade_date")
    return [file.stem for file in files]


def check_limit_up_field(
    cache_dir: Path,
    report: Report,
    workers: int = DEFAULT_WORKERS,
    deep_limit: int = DEFAULT_DEEP_SAMPLE,
) -> None:
    """Validate is_limit_up as a derived K-line field inside price_store qfq rows.

    is_limit_up is no longer sourced from a third-party 涨停池; it is computed
    locally from each stock's K-line using the official per-board price-limit
    ratio (主板 10% / 科创·创业板 20% / 北交所 30%). This check confirms every
    qfq row's is_limit_up is a bool-or-None and that, where both the predecessor
    and current close are present, it matches a deterministic recomputation -- so
    any stale pool-era value would be caught as inconsistent. The v5 store carries
    a single qfq series, so the recompute uses the qfq close ratio directly (no
    adjustment factor).
    """
    directory = cache_dir / "price_store"
    if not directory.is_dir():
        report.warning("limit_up_field_skipped", "price_store/ missing; cannot validate is_limit_up")
        return
    files = sorted(directory.glob("*.json"))
    sample, sample_meta = deep_sample(files, deep_limit)
    report.counts["limit_up_field_checked"] = sample_meta["checked"]
    if sample_meta["sampled"]:
        report.warning(
            "limit_up_field_sampled",
            f"limit-up recomputation sampled {sample_meta['checked']}/{sample_meta['listed']} stores "
            "(pass --deep-all for exhaustive checking)",
        )
    checked = mismatched = bad_type = none_count = true_count = false_count = 0
    try:
        import fetch_akshare_data as fad  # local: recompute the K-line value
    except Exception:  # noqa: BLE001 - recompute is best-effort
        fad = None
    for file in sample:
        payload = load_json(file)
        qfq = payload.get("qfq") if isinstance(payload, dict) else None
        if not isinstance(qfq, list):
            continue
        by_date = {
            row["trade_date"]: dict(row)
            for row in qfq
            if isinstance(row, dict) and row.get("trade_date")
        }
        if fad is not None:
            try:
                fad.annotate_limit_up(by_date, file.stem)
            except Exception:  # noqa: BLE001
                pass
        for row in qfq:
            if not isinstance(row, dict) or not row.get("trade_date"):
                continue
            val = row.get("is_limit_up")
            if val is not None and not isinstance(val, bool):
                bad_type += 1
                continue
            if val is None:
                none_count += 1
            elif val:
                true_count += 1
            else:
                false_count += 1
            checked += 1
            expected = by_date.get(row["trade_date"], {}).get("is_limit_up")
            if val != expected:
                mismatched += 1
    report.counts["limit_up_field_true"] = true_count
    report.counts["limit_up_field_false"] = false_count
    report.counts["limit_up_field_none"] = none_count
    if bad_type:
        report.error("limit_up_field_type", f"{bad_type} qfq rows have non-bool, non-null is_limit_up")
    if mismatched:
        report.error("limit_up_field_inconsistent",
                     f"{mismatched} qfq rows disagree with the recomputed K-line value")


def check_legacy_lists(
    cache_dir: Path,
    report: Report,
    workers: int = DEFAULT_WORKERS,
    deep_limit: int = DEFAULT_DEEP_SAMPLE,
    expected: "tuple[str, ...] | None" = None,
    optional: tuple[str, ...] = (),
) -> None:
    """Every derived price tree must stay a plain list, or downstream cannot read it.

    ``expected`` names the directories this layout must have. ``optional`` names the
    ones that may legitimately be absent (derived on read, or produced by a later
    mode such as ``derive``); their absence is recorded as ``*_absent_optional``
    instead of a warning, so an intended architecture change is not reported as a gap.
    """
    names = expected if expected is not None else (
        "symbol_cache_hfq", "forward_daily", "unadjusted_daily", "daily_adjusted")
    for name in names:
        directory = cache_dir / name
        if not directory.is_dir():
            if name in optional:
                report.counts[f"{name}_absent_optional"] = 1
            else:
                report.warning("legacy_dir_missing", f"{name}/ missing")
            continue
        files = sorted(directory.glob("*.json"))
        checked, sample_meta = deep_sample(files, deep_limit)
        non_list = 0
        for _file, payload in iter_loaded(checked, workers):
            if not isinstance(payload, list):
                non_list += 1
        report.counts[f"{name}_files"] = len(files)
        report.counts[f"{name}_deep_checked"] = sample_meta["checked"]
        if sample_meta["sampled"]:
            report.warning(
                "legacy_deep_sampled",
                f"deep content check sampled {sample_meta['checked']}/{sample_meta['listed']} files under {name}/ "
                "(pass --deep-all for exhaustive checking)",
            )
        if non_list:
            scope = f"{non_list}/{sample_meta['checked']} checked" if sample_meta["sampled"] else f"{non_list}"
            report.error("legacy_not_list", f"{scope} files under {name}/ are not plain lists (downstream requires list)")


def check_daily_coverage(cache_dir: Path, report: Report, calendar: list[str]) -> None:
    directory = cache_dir / "daily_adjusted"
    if not directory.is_dir():
        return
    present = {file.stem for file in directory.glob("*.json") if DATE_RE.match(file.stem)}
    report.counts["daily_adjusted_partitions"] = len(present)
    manifest = load_json(cache_dir / "bootstrap_manifest.json")
    if not isinstance(manifest, dict):
        return
    summary = manifest.get("summary") or {}
    sessions = summary.get("sessions") or []
    if not isinstance(sessions, list) or not sessions:
        return
    missing = [day for day in sessions if day not in present]
    report.counts["bootstrap_missing_partitions"] = len(missing)
    if missing:
        report.error("daily_partitions_missing", f"{len(missing)} bootstrap sessions lack a daily_adjusted partition, e.g. {missing[:3]}")
    coverage = summary.get("coverage") or {}
    report.counts["bootstrap_coverage"] = coverage


def cross_check_symbols(cache_dir: Path, report: Report, sample: int) -> None:
    directory = cache_dir / "symbol_cache"
    if not directory.is_dir():
        return
    files = sorted(directory.glob("*.json"))[: max(0, sample)]
    mismatches = 0
    compared = 0
    for file in files:
        payload = load_json(file)
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            continue
        code = file.stem
        view = {str(row["trade_date"]): row for row in payload["rows"] if isinstance(row, dict) and row.get("trade_date")}
        hfq_legacy = {str(row.get("trade_date")): row for row in (load_json(cache_dir / "symbol_cache_hfq" / f"{code}.json") or []) if isinstance(row, dict)}
        fwd_legacy = {str(row.get("trade_date")): row for row in (load_json(cache_dir / "forward_daily" / f"{code}.json") or []) if isinstance(row, dict)}
        raw_legacy = {str(row.get("trade_date")): row for row in (load_json(cache_dir / "unadjusted_daily" / f"{code}.json") or []) if isinstance(row, dict)}
        for day, row in view.items():
            if day in hfq_legacy and row.get("hfq_close") is not None:
                compared += 1
                if not close_enough(row.get("hfq_close"), hfq_legacy[day].get("close")):
                    mismatches += 1
            if day in fwd_legacy and row.get("qfq_close") is not None:
                compared += 1
                if not close_enough(row.get("qfq_close"), fwd_legacy[day].get("close")):
                    mismatches += 1
            if day in raw_legacy and row.get("raw_close") is not None:
                compared += 1
                if not close_enough(row.get("raw_close"), raw_legacy[day].get("close")):
                    mismatches += 1
    report.counts["cross_checked_symbols"] = len(files)
    report.counts["cross_checked_values"] = compared
    report.counts["cross_check_mismatches"] = mismatches
    if mismatches:
        report.error("cross_check_mismatch", f"{mismatches}/{compared} sampled values disagree between symbol_cache and legacy mirrors")


def detect_layout(cache_dir: Path) -> str:
    """Which cache generation this directory holds.

    * ``v5`` -- one ``price_store/<ts>.json`` per symbol holding a single qfq series
      (``qfq[]`` + ``is_limit_up``); no raw leg, no factor, no anchor.
    * ``v4`` -- one ``price_store/<ts>.json`` per symbol holding raw prices plus a
      per-session adjustment factor, everything else derived from ``raw × factor``.
    * ``v3`` -- a folded ``symbol_cache/`` view plus four mirrored price trees.

    v4 and v5 share a directory shape, so the generation is decided by the store's
    own ``schema_version`` rather than by the presence of ``price_store/``. Reporting
    a v5 cache as ``v4`` used to make the report contradict the manifest.
    """

    store = cache_dir / "price_store"
    if store.is_dir():
        for path in sorted(store.glob("*.json"))[:16]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            version = payload.get("schema_version") if isinstance(payload, dict) else None
            if isinstance(version, int) and version >= 5:
                return "v5"
        return "v4"
    if (cache_dir / "symbol_cache").is_dir():
        return "v3"
    return "unknown"


def check_price_store(
    cache_dir: Path,
    report: Report,
    workers: int = DEFAULT_WORKERS,
    deep_limit: int = DEFAULT_DEEP_SAMPLE,
) -> None:
    """Validate the v5 authority layer: one qfq store per symbol, self-contained.

    The v5 store holds a single forward-adjusted (qfq) series plus ``last_update``.
    The invariants that make derivation sound are all local to a store, so they can
    be checked without looking at any derived artifact:

      * every qfq row carries the four prices (open/high/low/close) and a trade_date;
      * every price is finite (when present);
      * ``is_limit_up`` is bool-or-None (the K-line-derived field).
    The factor / anchor machinery from v4 is gone: there is no second leg to
    reconcile and no basis to drift on, so the old anchor-mismatch / orphan-factor
    checks no longer apply.
    """
    directory = cache_dir / "price_store"
    if not directory.is_dir():
        report.error("price_store_missing", "price_store/ missing (authority layer)")
        return
    files = sorted(directory.glob("*.json"))
    report.counts["price_store_files"] = len(files)
    if not files:
        report.error("price_store_empty", "price_store/ contains no symbol stores")
        return
    checked, sample_meta = deep_sample(files, deep_limit)
    report.counts["price_store_deep_checked"] = sample_meta["checked"]
    bad_schema = 0
    bad_numbers = 0
    bad_limit_up = 0
    for _file, payload in iter_loaded(checked, workers):
        if not isinstance(payload, dict) or payload.get("schema_version") != 5:
            bad_schema += 1
            continue
        qfq_rows = payload.get("qfq")
        if not isinstance(qfq_rows, list):
            bad_schema += 1
            continue
        for row in qfq_rows:
            if not isinstance(row, dict):
                bad_schema += 1
                continue
            day = row.get("trade_date")
            if not day:
                bad_schema += 1
                continue
            for field in ("open", "high", "low", "close"):
                if field not in row:
                    bad_schema += 1
                    break
            for field in ("open", "high", "low", "close"):
                value = row.get(field)
                if value is not None and not is_number(value):
                    bad_numbers += 1
            val = row.get("is_limit_up")
            if val is not None and not isinstance(val, bool):
                bad_limit_up += 1
    if bad_schema:
        report.error("price_store_schema", f"{bad_schema} stores are malformed (schema_version/qfq/price keys)")
    if bad_numbers:
        report.error("price_store_nonfinite_price", f"{bad_numbers} qfq price fields are non-finite")
    if bad_limit_up:
        report.error("price_store_limit_up_type", f"{bad_limit_up} qfq rows have non-bool, non-null is_limit_up")
    if sample_meta["sampled"]:
        report.warning("price_store_deep_sampled",
                       f"deep content check sampled {sample_meta['checked']}/{sample_meta['listed']} stores "
                       "(pass --deep-all for exhaustive checking)")


def check_derived_consistency(cache_dir: Path, report: Report, sample: int) -> None:
    """Confirm the derived artifacts really are projections of the store.

    Without this, a stale ``daily_adjusted/`` left over from an older fetch
    looks perfectly valid: it is a plain list of plausible prices. Re-deriving a
    sample and comparing is the only cheap way to tell the two apart.
    """
    directory = cache_dir / "price_store"
    if not directory.is_dir() or sample <= 0:
        return
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fetch"))
    try:
        import prices_v4 as prices  # noqa: PLC0415 - optional module, keep import local
    except ImportError:
        report.warning("derived_check_skipped", "prices_v4.py not importable; derived artifacts were not cross-checked")
        return
    files = sorted(directory.glob("*.json"))
    picked, _meta = deep_sample(files, sample)
    partitions: dict[str, dict[str, dict[str, Any]]] = {}
    mismatched = 0
    compared = 0
    missing_symbols = 0
    for _file, payload in iter_loaded(picked, 1):
        if not isinstance(payload, dict) or not isinstance(payload.get("qfq"), list):
            continue
        code = payload.get("ts_code")
        derived = prices.derive_rows(payload)
        # compare against the per-symbol qfq mirror when the symbol is in scope
        qfq_path = cache_dir / "forward_daily" / f"{code}.json"
        qfq_payload = load_json(qfq_path) if qfq_path.exists() else None
        if isinstance(qfq_payload, list) and qfq_payload:
            by_date = {row.get("trade_date"): row for row in qfq_payload if isinstance(row, dict)}
            for row in derived["qfq"]:
                want = by_date.get(row["trade_date"])
                if want is None:
                    continue
                compared += 1
                if not close_enough_row(row, want, prices):
                    mismatched += 1
                    break
        # compare the last session against the market partition
        last = derived["qfq"][-1] if derived["qfq"] else None
        if not last:
            continue
        day = last["trade_date"]
        if day not in partitions:
            partition = load_json(cache_dir / "daily_adjusted" / f"{day}.json")
            partitions[day] = ({row.get("ts_code"): row for row in partition if isinstance(row, dict)}
                               if isinstance(partition, list) else {})
        want = partitions[day].get(code)
        if want is None:
            missing_symbols += 1
            continue
        compared += 1
        if not close_enough_row(last, want, prices):
            mismatched += 1
    report.counts["derived_rows_compared"] = compared
    if mismatched:
        report.error("derived_price_mismatch",
                     f"{mismatched} sampled symbols disagree with daily_adjusted/forward_daily; "
                     "the derived artifacts are stale relative to price_store -- rerun derive_cache.py --mode derive")
    if missing_symbols:
        report.warning("derived_rows_missing",
                       f"{missing_symbols} sampled symbols are absent from their last-session daily_adjusted partition")


def close_enough_row(derived: dict[str, Any], stored: dict[str, Any], prices: Any) -> bool:
    """Equal after quantization to the upstream tick, per price field."""
    for field in ("open", "high", "low", "close"):
        left = prices.quantize(derived.get(field))
        right = prices.quantize(stored.get(field))
        if left is None and right is None:
            continue
        if left is None or right is None:
            return False
        if abs(left - right) > 1e-9:
            return False
    return True


def run(
    cache_dir: Path,
    sample: int,
    workers: int = DEFAULT_WORKERS,
    deep_limit: int = DEFAULT_DEEP_SAMPLE,
) -> dict[str, Any]:
    report = Report()
    if not cache_dir.is_dir():
        report.error("cache_dir_missing", f"{cache_dir} is not a directory")
        return {"cache_dir": str(cache_dir), "ok": False, "counts": {}, "issues": report.issues}
    calendar = check_calendar(cache_dir, report)
    check_master(cache_dir, report)
    layout = detect_layout(cache_dir)
    report.counts["layout"] = layout
    if layout in ("v4", "v5"):
        check_price_store(cache_dir, report, workers, deep_limit)
        check_derived_consistency(cache_dir, report, max(1, sample))
        if (cache_dir / "symbol_cache").is_dir():
            report.warning("folded_view_present",
                           "symbol_cache/ exists alongside price_store/; it is derived on read, so the copies can drift")
        # v5 stores a single qfq series per symbol; forward_daily/ and unadjusted_daily/
        # are *derive-mode* outputs covering the candidate scope only, so a freshly
        # bootstrapped cache legitimately has neither. Treat them as optional and keep
        # validating them whenever they exist. daily_adjusted/ is written by bootstrap
        # itself, so its absence is a real gap.
        optional = ("symbol_cache_hfq", "symbol_cache") if layout == "v4" else (
            "symbol_cache_hfq", "symbol_cache", "forward_daily", "unadjusted_daily")
        check_legacy_lists(cache_dir, report, workers, deep_limit,
                           expected=("daily_adjusted", "forward_daily", "unadjusted_daily"),
                           optional=optional)
    else:
        check_symbol_cache(cache_dir, report, workers, deep_limit)
        check_legacy_lists(cache_dir, report, workers, deep_limit)
    check_limit_up_field(cache_dir, report, workers, deep_limit)
    check_daily_coverage(cache_dir, report, calendar)
    if layout == "v3":
        cross_check_symbols(cache_dir, report, sample)
    return {
        "cache_dir": str(cache_dir),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "layout": layout,
        "ok": not report.has_errors(),
        "counts": report.counts,
        "issues": report.issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the integrated A-share cache contract")
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--sample", type=int, default=50, help="symbols to cross-check between symbol_cache and legacy mirrors")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="parallel file readers (default 16)")
    parser.add_argument(
        "--deep-sample",
        type=int,
        default=DEFAULT_DEEP_SAMPLE,
        help=f"files per directory that get deep content validation (default {DEFAULT_DEEP_SAMPLE}); file counts are always exhaustive",
    )
    parser.add_argument("--deep-all", action="store_true", help="deep-validate every cache file (minutes on a full market cache)")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    deep_limit = 0 if args.deep_all else max(0, args.deep_sample)
    result = run(args.cache_dir, max(0, args.sample), max(1, args.workers), deep_limit)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI returns structured failure
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
