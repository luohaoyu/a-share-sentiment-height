#!/usr/bin/env python3
"""用历史收盘涨停池同步 price_store 中的显式涨停状态。

这是涨停状态的权威派生入口：对每个成功取得涨停池的交易日，池内股票标记为
True，其他有日 K 的股票标记为 False；接口失败的交易日保持原值并在清单中报错，
禁止把未知状态静默降级为 False。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def normalize_code(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())[-6:].zfill(6)
    if digits.startswith(("600", "601", "603", "605", "688")):
        return f"{digits}.SH"
    if digits.startswith(("000", "001", "002", "003", "300", "301")):
        return f"{digits}.SZ"
    if digits.startswith(("43", "83", "87", "92")):
        return f"{digits}.BJ"
    return digits


def fetch_day(ak: Any, day: str, retries: int) -> tuple[set[str], list[dict[str, Any]]]:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            frame = ak.stock_zt_pool_em(date=day.replace("-", ""))
            if frame is None:
                raise RuntimeError("接口返回 None")
            rows = frame.to_dict(orient="records")
            codes = {normalize_code(row.get("代码")) for row in rows}
            codes.discard("")
            return codes, rows
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(min(8.0, 1.5 ** attempt))
    raise RuntimeError(f"{day} 涨停池获取失败: {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description="用历史涨停池同步 price_store 涨停状态")
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        print(f"缺少 akshare: {exc}", file=sys.stderr)
        return 2

    calendar_payload = read_json(args.cache_dir / "calendar.json")
    sessions = sorted({str(row.get("cal_date") or row.get("trade_date")) for row in calendar_payload})
    sessions = [day for day in sessions if args.start_date <= day <= args.end_date]
    if not sessions:
        print("指定区间没有交易日", file=sys.stderr)
        return 2

    pools: dict[str, set[str]] = {}
    source_rows: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []
    for index, day in enumerate(sessions, start=1):
        try:
            codes, rows = fetch_day(ak, day, args.retries)
            pools[day] = codes
            source_rows[day] = rows
            print(f"[{index}/{len(sessions)}] {day}: {len(codes)}")
        except Exception as exc:  # noqa: BLE001
            failures.append({"date": day, "error": str(exc)})
            print(f"[{index}/{len(sessions)}] {day}: FAILED {exc}", file=sys.stderr)

    if failures:
        manifest = {
            "ok": False,
            "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
            "source": "akshare.stock_zt_pool_em (Eastmoney historical close limit-up pool)",
            "sessions_requested": sessions,
            "sessions_succeeded": sorted(pools),
            "failures": failures,
            "stores_updated": 0,
        }
        atomic_write(args.manifest, manifest)
        print("存在失败交易日，未修改任何 price_store 文件", file=sys.stderr)
        return 1

    changed_rows = 0
    true_rows = 0
    stores_updated = 0
    store_dir = args.cache_dir / "price_store"
    for path in sorted(store_dir.glob("*.json")):
        store = read_json(path)
        ts_code = str(store.get("ts_code") or path.stem)
        changed = False
        for row in store.get("qfq") or []:
            day = str(row.get("trade_date") or "")
            if day not in pools:
                continue
            expected = ts_code in pools[day]
            if row.get("is_limit_up") is not expected:
                changed_rows += 1
                changed = True
            row["is_limit_up"] = expected
            row["limit_up_source"] = "eastmoney_historical_close_pool"
            if expected:
                true_rows += 1
        if changed:
            stores_updated += 1
        atomic_write(path, store)

    raw_dir = args.cache_dir / "limit_up_pool"
    for day, rows in source_rows.items():
        atomic_write(raw_dir / f"{day}.json", {
            "date": day,
            "source": "akshare.stock_zt_pool_em",
            "count": len(rows),
            "rows": rows,
        })

    manifest = {
        "ok": True,
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "akshare.stock_zt_pool_em (Eastmoney historical close limit-up pool)",
        "definition": "成功交易日池内=True；池外且存在日K=False；失败交易日整批拒绝写入",
        "sessions_requested": sessions,
        "sessions_succeeded": sessions,
        "stores_updated": stores_updated,
        "changed_rows": changed_rows,
        "true_rows": true_rows,
        "failures": [],
    }
    atomic_write(args.manifest, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
