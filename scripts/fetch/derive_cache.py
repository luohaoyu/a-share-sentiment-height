#!/usr/bin/env python3
"""Materialize the v4 price store, or rebuild the legacy mirrors from it.

Modes
-----
factorize
    Migrate a schema-v3 cache without touching the network: read the folded
    ``symbol_cache/<ts>.json`` views, split the hfq/qfq/raw blocks into a
    ``price_store/<ts>.json`` (raw prices + factor + ex-rights events), and
    record which symbols carry a qfq scope. Works in place or into ``--out-dir``.

derive
    Rebuild every derived artifact from the store: ``daily_adjusted/``,
    ``forward_daily/``, ``unadjusted_daily/`` and, on request, the
    ``symbol_cache/`` view. Downstream CLIs read these unchanged; they no longer
    carry independent information.

verify
    Compare derived artifacts against a baseline mirror tree and report the
    deviation distribution per field. ``close`` must match exactly;
    ``open/high/low`` may differ by at most one tick, which is the precision
    upstream discards when it prints two decimals.

Examples
--------
    # v3 -> v4 into a fresh directory (source untouched)
    python derive_cache.py --mode factorize --cache-dir cache/akshare --out-dir cache/akshare_v4

    # v4 -> derived artifacts
    python derive_cache.py --mode derive --cache-dir cache/akshare_v4 --scope-from cache/akshare

    # prove the derivation reproduces the old mirrors
    python derive_cache.py --mode verify --cache-dir cache/akshare_v4 --baseline-dir cache/akshare
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prices_v4 as P  # noqa: E402


def _view_rows(cache_dir: Path, ts_code: str) -> tuple[list[dict[str, Any]], "str | None"]:
    payload = P.load_json(cache_dir / "symbol_cache" / f"{ts_code}.json")
    if isinstance(payload, dict):
        rows = payload.get("rows")
        return ([row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else [],
                payload.get("list_date"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)], None
    return [], None


SHARED_ASSETS = (
    "calendar.json",
    "calendar_upstream.json",
    "stock_basic.json",
    "st",
    "bootstrap_manifest.json",
    "waves_manifest.json",
    "snapshot_manifests",
)


def _copy_shared_assets(cache_dir: Path, out_dir: Path) -> list[str]:
    """Make the v4 directory self-contained.

    Only the price layer is rebuilt by factorize; the calendar, stock master and
    ST name snapshots are generation-independent facts. is_limit_up is a derived
    field inside price_store (computed from the K-line at fetch time), so there
    is no separate limit-up pool layer to copy. A v4 cache that has to read them
    from a sibling v3 directory is not a cache, it is a patch -- so they are
    copied over when migrating out of place.
    """
    if cache_dir.resolve() == out_dir.resolve():
        return []
    copied: list[str] = []
    for name in SHARED_ASSETS:
        source = cache_dir / name
        target = out_dir / name
        if not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        copied.append(name)
    return copied


def factorize(cache_dir: Path, out_dir: Path, limit: "int | None" = None, with_views: bool = False) -> dict[str, Any]:
    """Split a v3 cache's per-symbol view into the v5 qfq store."""
    view_dir = cache_dir / "symbol_cache"
    codes = sorted(path.stem for path in view_dir.glob("*.json")) if view_dir.is_dir() else []
    if not codes:
        raise SystemExit(f"no symbol_cache/*.json under {cache_dir}; cannot factorize")
    out_dir.mkdir(parents=True, exist_ok=True)

    qfq_scope: list[str] = []
    sessions: set[str] = set()
    materials: Counter = Counter()
    empty_qfq = 0
    for index, code in enumerate(codes, start=1):
        rows, list_date = _view_rows(cache_dir, code)
        if not rows:
            continue
        qfq_rows: list[dict[str, Any]] = []
        price_mode = None
        has_qfq = False
        for row in rows:
            day = row.get("trade_date")
            if not day:
                continue
            sessions.add(str(day))
            price_mode = price_mode or row.get("price_mode")
            # v5: the authoritative series IS the qfq series, so a v3 view is
            # factorized by keeping its qfq_* prices directly -- there is no
            # second raw/hfq leg and no factor to reconcile.
            qfq_rows.append({
                "trade_date": day,
                "open": row.get("qfq_open"),
                "high": row.get("qfq_high"),
                "low": row.get("qfq_low"),
                "close": row.get("qfq_close"),
                "is_limit_up": row.get("is_limit_up"),
            })
            if row.get("qfq_close") is not None:
                has_qfq = True
        store = P.build_store(code, list_date, qfq_rows, price_mode)
        if not store["qfq"]:
            empty_qfq += 1
        P.write_store(out_dir, store)
        if has_qfq:
            qfq_scope.append(code)
        materials["qfq_rows"] += len(store["qfq"])
        if with_views:
            P.atomic_write_json(out_dir / "symbol_cache" / f"{code}.json", {
                "schema_version": P.SCHEMA_VERSION,
                "ts_code": code,
                "list_date": list_date,
                "derived_from": [P.STORE_DIR],
                "rows": P.derive_rows(store)["view"],
            })
        if index % 1000 == 0:
            print(f"  factorize {index}/{len(codes)}", file=sys.stderr)
        if limit and index >= limit:
            break

    manifest = {
        "schema_version": P.SCHEMA_VERSION,
        "mode": "factorize",
        "source_cache": str(cache_dir),
        "shared_assets_copied": _copy_shared_assets(cache_dir, out_dir),
        "sessions": sorted(sessions),
        "session_count": len(sessions),
        "symbol_count": len(P.list_symbols(out_dir)),
        "qfq_scope": sorted(qfq_scope),
        "symbols_without_qfq": empty_qfq,
        "counts": dict(materials),
        "layers": {
            "authority": [P.STORE_DIR],
            "derived": ["daily_adjusted", "forward_daily", "unadjusted_daily", "symbol_cache"],
        },
    }
    P.atomic_write_json(out_dir / "cache_v4_manifest.json", manifest)
    return manifest


def _qfq_scope(cache_dir: Path, scope_from: "Path | None", explicit: list[str]) -> set[str]:
    if explicit:
        return {code.strip() for code in explicit if code.strip()}
    directory = (scope_from or cache_dir) / "forward_daily"
    if not directory.is_dir():
        return set()
    return {path.stem for path in directory.glob("*.json") if path.stat().st_size > 2}


def derive(
    cache_dir: Path,
    out_dir: Path,
    scope: set[str],
    with_views: bool,
    sessions_filter: "list[str] | None" = None,
) -> dict[str, Any]:
    """Rebuild every derived artifact from the v5 qfq store."""
    all_codes = P.list_symbols(cache_dir)
    if not all_codes:
        raise SystemExit(f"no {P.STORE_DIR}/*.json under {cache_dir}; run --mode factorize first")
    # Bootstrap already materializes daily_adjusted. When an explicit candidate
    # scope is supplied, derive only the candidate views instead of reopening all
    # 5,000+ stores and rebuilding identical full-market partitions.
    scope_only = bool(scope) and out_dir == cache_dir and (out_dir / "daily_adjusted").is_dir()
    codes = sorted(scope & set(all_codes)) if scope_only else all_codes
    out_dir.mkdir(parents=True, exist_ok=True)

    partitions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stats: Counter = Counter()
    empty_qfq: list[str] = []
    qfq_written = 0
    for index, code in enumerate(codes, start=1):
        store = P.read_store(cache_dir, code)
        if not store:
            continue
        # v5 stores carry a single qfq series; there is no factor / anchor to
        # check (the old "no_factor" probe guarded the v4 raw+hfq pair).
        if not store.get("qfq"):
            empty_qfq.append(code)
        derived = P.derive_rows(store)
        if not scope_only:
            for row in derived["hfq"]:
                if sessions_filter and row["trade_date"] not in sessions_filter:
                    continue
                partitions[row["trade_date"]].append(row)
        # unadjusted_daily + forward_daily stay scoped: only the qfq candidate set
        # consumes them, so materializing 5575 copies would recreate the waste v4 removes.
        if code in scope:
            P.atomic_write_json(out_dir / "unadjusted_daily" / f"{code}.json", derived["raw"])
            P.atomic_write_json(out_dir / "forward_daily" / f"{code}.json", derived["qfq"])
            qfq_written += 1
        if with_views:
            P.atomic_write_json(out_dir / "symbol_cache" / f"{code}.json", {
                "schema_version": P.SCHEMA_VERSION,
                "ts_code": code,
                "list_date": store.get("list_date"),
                "derived_from": [P.STORE_DIR],
                "rows": derived["view"],
            })
        stats["symbols"] += 1
        stats["rows"] += len(derived["raw"])
        if index % 1000 == 0:
            print(f"  derive {index}/{len(codes)}", file=sys.stderr)

    for day, rows in partitions.items():
        rows.sort(key=lambda row: row["ts_code"])
        P.atomic_write_json(out_dir / "daily_adjusted" / f"{day}.json", rows)

    manifest_path = cache_dir / "cache_v4_manifest.json"
    manifest = P.load_json(manifest_path) or {}
    manifest.update({
        "schema_version": P.SCHEMA_VERSION,
        "derived_at_mode": "derive",
        "sessions": sorted(partitions) if partitions else manifest.get("sessions", []),
        "session_count": len(partitions) if partitions else manifest.get("session_count", 0),
        "symbol_count": len(all_codes),
        "scope_only": scope_only,
        "qfq_scope": sorted(scope),
        "symbols_without_qfq": len(empty_qfq),
        "symbols_without_qfq_sample": empty_qfq[:5],
        "derived_counts": {
            "daily_adjusted_partitions": len(partitions) if partitions else manifest.get("derived_counts", {}).get("daily_adjusted_partitions", 0),
            "forward_daily": qfq_written,
            "unadjusted_daily": qfq_written,
            "symbol_cache_views": stats["symbols"] if with_views else 0,
            "rows": stats["rows"],
        },
    })
    P.atomic_write_json(manifest_path, manifest)
    return manifest


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------
def _compare_rows(
    derived: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    label: str,
    tolerance: float,
    report: dict[str, Any],
    codes: "set[str] | None" = None,
) -> None:
    def index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        out = {}
        for row in rows or []:
            if isinstance(row, dict) and row.get("trade_date"):
                if codes is not None and str(row.get("ts_code")) not in codes:
                    continue
                out[(str(row.get("ts_code")), str(row["trade_date"]))] = row
        return out

    left, right = index(baseline), index(derived)
    entry = report["series"].setdefault(label, Counter())
    entry["baseline_rows"] += len(left)
    entry["derived_rows"] += len(right)
    entry["missing_in_derived"] += len(set(left) - set(right))
    entry["extra_in_derived"] += len(set(right) - set(left))
    if set(left) != set(right) and label not in report["set_mismatch"]:
        report["set_mismatch"].append(label)
    buckets = report.setdefault("fields", {}).setdefault(label, {})
    for key in set(left) & set(right):
        for field in P.PRICE_FIELDS:
            want = P.quantize(left[key].get(field))
            got = P.quantize(right[key].get(field))
            bucket = buckets.setdefault(field, Counter())
            if want is None and got is None:
                bucket["both_null"] += 1
                continue
            if want is None or got is None:
                bucket["null_mismatch"] += 1
                continue
            delta = abs(want - got)
            bucket["compared"] += 1
            if delta <= 1e-12:
                bucket["exact"] += 1
            elif delta <= tolerance + 1e-9:
                bucket["one_tick"] += 1
            else:
                bucket["beyond_tolerance"] += 1
                if len(report["worst"]) < 5:
                    report["worst"].append({"series": label, "ts_code": key[0], "trade_date": key[1],
                                            "field": field, "baseline": want, "derived": got})


def verify(cache_dir: Path, baseline: Path, tolerance: float, sample: "int | None" = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": P.SCHEMA_VERSION,
        "cache_dir": str(cache_dir),
        "baseline_dir": str(baseline),
        "tolerance": tolerance,
        "series": {},
        "set_mismatch": [],
        "worst": [],
        "not_materialized": ["symbol_cache_hfq"],
    }
    codes = P.list_symbols(cache_dir)
    if sample:
        codes = codes[:sample]
    code_set = set(codes)
    coverage = Counter()
    for code in codes:
        store = P.read_store(cache_dir, code)
        if not store:
            continue
        derived = P.derive_rows(store)
        for series, directory in (("raw", "unadjusted_daily"), ("hfq", "symbol_cache_hfq"), ("qfq", "forward_daily")):
            baseline_path = baseline / directory / f"{code}.json"
            if not baseline_path.exists():
                coverage[f"{directory}_absent_in_baseline"] += 1
                continue
            baseline_rows = P.load_json(baseline_path)
            if not isinstance(baseline_rows, list):
                continue
            # prefer the artifact actually written by --mode derive; fall back to
            # the in-memory projection for series v4 deliberately stops storing
            materialized = cache_dir / directory / f"{code}.json"
            payload = P.load_json(materialized) if materialized.exists() else None
            rows = payload if isinstance(payload, list) else derived[series]
            _compare_rows(rows, baseline_rows, f"{directory}/<symbol>", tolerance, report, {code})
    # Partitions are compared as written artifacts, so the check covers exactly
    # what compute/ will read rather than a re-computation of it.
    derived_partitions = cache_dir / "daily_adjusted"
    for path in sorted((baseline / "daily_adjusted").glob("*.json")):
        baseline_rows = P.load_json(path)
        if not isinstance(baseline_rows, list):
            continue
        materialized = derived_partitions / path.name
        if not materialized.exists():
            coverage["daily_adjusted_partition_absent_in_derived"] += 1
            continue
        payload = P.load_json(materialized)
        if not isinstance(payload, list):
            continue
        _compare_rows(payload, baseline_rows, "daily_adjusted/<date>", tolerance, report, code_set)
    report["coverage"] = dict(coverage)
    report["ok"] = (
        not report["set_mismatch"]
        and all(counts.get("beyond_tolerance", 0) == 0 and counts.get("null_mismatch", 0) == 0
                for per_field in report["fields"].values() for counts in per_field.values())
    )
    return report


def _jsonable(report: dict[str, Any]) -> dict[str, Any]:
    out = dict(report)
    out["fields"] = {
        label: {field: dict(counts) for field, counts in per_field.items()}
        for label, per_field in report.get("fields", {}).items()
    }
    out["series"] = {label: dict(counts) for label, counts in report.get("series", {}).items()}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="v4 price store: factorize / derive / verify")
    parser.add_argument("--mode", choices=("factorize", "derive", "verify"), required=True)
    parser.add_argument("--cache-dir", required=True, type=Path, help="v3 source cache (factorize) or v4 cache (derive/verify)")
    parser.add_argument("--out-dir", type=Path, default=None, help="write target; defaults to --cache-dir")
    parser.add_argument("--baseline-dir", type=Path, default=None, help="verify: v3 cache holding the legacy mirrors")
    parser.add_argument("--scope-from", type=Path, default=None, help="derive: directory whose forward_daily defines the qfq scope")
    parser.add_argument("--qfq-symbols", nargs="*", default=[], help="derive: explicit qfq scope override")
    parser.add_argument("--with-views", action="store_true", help="also materialize symbol_cache/ views")
    parser.add_argument("--tolerance", type=float, default=0.0100001, help="verify: max absolute price delta (one tick)")
    parser.add_argument("--sample", type=int, default=None, help="verify/factorize: cap the symbol count")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    if args.mode == "factorize":
        out_dir = args.out_dir or args.cache_dir
        result = factorize(args.cache_dir, out_dir, args.sample, args.with_views)
    elif args.mode == "derive":
        out_dir = args.out_dir or args.cache_dir
        scope = _qfq_scope(args.cache_dir, args.scope_from, args.qfq_symbols)
        result = derive(args.cache_dir, out_dir, scope, args.with_views)
    else:
        if not args.baseline_dir:
            parser.error("--mode verify requires --baseline-dir")
        result = _jsonable(verify(args.cache_dir, args.baseline_dir, args.tolerance, args.sample))

    if args.json_out:
        P.atomic_write_json(args.json_out, result)
    print(json.dumps(result, ensure_ascii=False, indent=1)[:4000])
    if args.mode == "verify" and not result.get("ok"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
