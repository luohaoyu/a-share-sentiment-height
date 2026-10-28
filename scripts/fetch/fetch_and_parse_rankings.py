#!/usr/bin/env python3
"""Fetch and normalize historical 10-day ranking pages.

Deterministic responsibilities:
- fetch one ranking page per supplied trading date;
- cache raw HTML and record request/parse counts;
- parse date, code, name, market label, and pct_10d;
- optionally resolve listing dates from stock detail pages.

The script does not decide whether a source is complete or trustworthy. The
calling agent must record source limitations and choose the source first.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def read_or_fetch(url: str, cache_dir: Path, timeout: int = 30) -> tuple[str, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_")
    cache_path = cache_dir / f"{safe}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace"), "cache"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "ignore")
    cache_path.write_text(body, encoding="utf-8")
    return body, "network"


def parse_rank_page(day: str, text: str, base_detail_url: str) -> list[dict]:
    starts = list(re.finditer(r'<div class="qjzf-stock-line" title="([^"]+)">', text))
    rows: list[dict] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[match.start():end]
        code = match.group(1).split(".")[0].strip().lower()
        market_match = re.search(r'class="qjzf-market">\s*([^<]+)', block)
        name_match = re.search(r'class="qjzf-name"[^>]*>\s*<a[^>]*>([^<]+)</a>', block, re.S)
        pct_match = re.search(r'class="qjzf-change[^>]*>[\s\S]*?<span>([-+]?\d+(?:\.\d+)?)</span>', block)
        if not (market_match and name_match and pct_match and code):
            continue
        rows.append({
            "date": day,
            "code": code,
            "market_label": clean_text(market_match.group(1)),
            "name": clean_text(name_match.group(1)),
            "pct": float(pct_match.group(1)),
            "url": f"{base_detail_url.rstrip('/')}/gupiao-{code}.html",
        })
    return rows


def parse_listing_date(text: str) -> str | None:
    patterns = [
        r"上市日期</dt>\s*<dd>(\d{4}-\d{2}-\d{2})</dd>",
        r"上市日期[^0-9]*(\d{4}-\d{2}-\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return match.group(1)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and parse historical 10-day ranking pages")
    parser.add_argument("--dates", nargs="+", required=True, help="ISO trading dates")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--rank-base-url", default="https://dabanke.com/qjzf-{yyyymmdd}.html?days=10&side=rise")
    parser.add_argument("--detail-base-url", default="https://dabanke.com")
    parser.add_argument("--resolve-listing", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = args.output_dir / "raw_cache"
    all_rows: list[dict] = []
    manifest: list[dict] = []
    for day in args.dates:
        url = args.rank_base_url.format(yyyymmdd=day.replace("-", ""), date=day)
        record = {"date": day, "url": url, "status": "failed", "source": None, "rows": 0}
        try:
            text, source = read_or_fetch(url, cache_dir, args.timeout)
            rows = parse_rank_page(day, text, args.detail_base_url)
            all_rows.extend(rows)
            record.update({"status": "ok", "source": source, "rows": len(rows)})
        except Exception as exc:  # record failure; do not fabricate rows
            record["error"] = f"{type(exc).__name__}: {exc}"
        manifest.append(record)

    listing: dict[str, str | None] = {}
    if args.resolve_listing:
        for row in all_rows:
            code = row["code"]
            if code in listing:
                continue
            try:
                text, _ = read_or_fetch(row["url"], cache_dir, args.timeout)
                listing[code] = parse_listing_date(text)
            except Exception:
                listing[code] = None
        for row in all_rows:
            row["listing_date"] = listing.get(row["code"])

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dates": args.dates,
        "rows": all_rows,
        "manifest": manifest,
        "source": {"rank_base_url": args.rank_base_url, "detail_base_url": args.detail_base_url},
        "warning": "解析结果只代表页面实际返回内容；完整性、展示阈值和数据口径必须由上层流程核验。",
    }
    output = args.output_dir / "normalized_candidates.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "dates": len(args.dates), "rows": len(all_rows), "failed_dates": sum(x["status"] != "ok" for x in manifest)}, ensure_ascii=False))
    return 0 if all(x["status"] == "ok" for x in manifest) else 2


if __name__ == "__main__":
    raise SystemExit(main())
