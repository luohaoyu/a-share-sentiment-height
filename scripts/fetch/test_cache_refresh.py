"""Offline contract tests for cache_refresh.py.

Run with the managed venv:
    python scripts/fetch/test_cache_refresh.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# make the sibling module importable when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cache_refresh import needs_refresh, sessions_to_fetch  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
# A contiguous trading calendar (no weekends), truncated at the observation date.
CAL = [
    "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
    "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
]
CAL_SET = set(CAL)


def is_trading_day(d: str) -> bool:
    return d in CAL_SET


def session_open(iso: str) -> datetime:
    y, m, dd = (int(x) for x in iso.split("-"))
    return datetime(y, m, dd, 9, 30)


# Observation "now": 2026-10-26 18:00 (today is a trading day, after open).
NOW = datetime(2026, 10, 26, 18, 0)


def run_sessions(cache_first, cache_last, last_update, req_first, req_last):
    return sessions_to_fetch(
        cache_first, cache_last, last_update, req_first, req_last,
        NOW, CAL, is_trading_day, session_open,
    )


def run_refresh(cache_first, cache_last, last_update, req_first, req_last):
    return needs_refresh(
        cache_first, cache_last, last_update, req_first, req_last,
        NOW, CAL, is_trading_day, session_open,
    )


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------
def test_empty_cache_fetches_whole_window():
    out = run_sessions(None, None, None, "2026-10-20", "2026-10-26")
    assert out == ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"], out
    assert run_refresh(None, None, None, "2026-10-20", "2026-10-26") is True


def test_request_fully_inside_no_fetch():
    # cache [10-19, 10-26]; request [10-20, 10-23] fully inside -> nothing
    out = run_sessions("2026-10-19", "2026-10-26", None, "2026-10-20", "2026-10-23")
    assert out == [], out
    assert run_refresh("2026-10-19", "2026-10-26", None, "2026-10-20", "2026-10-23") is False


def test_backfill_earlier_only():
    # cache [10-21, 10-26]; request [10-19, 10-23] -> only 10-19, 10-20 (earlier)
    out = run_sessions("2026-10-21", "2026-10-26", None, "2026-10-19", "2026-10-23")
    assert out == ["2026-10-19", "2026-10-20"], out
    assert run_refresh("2026-10-21", "2026-10-26", None, "2026-10-19", "2026-10-23") is True


def test_append_later_trading_days_excludes_weekend():
    # cache [10-19, 10-23]; request [10-19, 10-30]
    # 10-24/10-25 are weekend -> not in calendar -> excluded automatically.
    # 10-26..10-30 are trading days after cache_last -> fetched.
    out = run_sessions("2026-10-19", "2026-10-23", None, "2026-10-19", "2026-10-30")
    assert out == ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"], out
    assert run_refresh("2026-10-19", "2026-10-23", None, "2026-10-19", "2026-10-30") is True


def test_d_eq_b_today_last_update_after_open_no_fetch():
    # cache last == today (10-26); last_update 18:00 >= open 09:30 -> no fetch
    out = run_sessions("2026-10-19", "2026-10-26", datetime(2026, 10, 26, 18, 0),
                       "2026-10-19", "2026-10-26")
    assert out == [], out
    assert run_refresh("2026-10-19", "2026-10-26", datetime(2026, 10, 26, 18, 0),
                       "2026-10-19", "2026-10-26") is False


def test_d_eq_b_today_last_update_before_open_fetch_today():
    # cache last == today; last_update 08:00 < open 09:30 -> re-fetch today
    out = run_sessions("2026-10-19", "2026-10-26", datetime(2026, 10, 26, 8, 0),
                       "2026-10-19", "2026-10-26")
    assert out == ["2026-10-26"], out
    assert run_refresh("2026-10-19", "2026-10-26", datetime(2026, 10, 26, 8, 0),
                       "2026-10-19", "2026-10-26") is True


def test_d_eq_b_today_null_last_update_fetch_today():
    # cache last == today; last_update unknown -> assume stale -> fetch today
    out = run_sessions("2026-10-19", "2026-10-26", None, "2026-10-19", "2026-10-26")
    assert out == ["2026-10-26"], out


def test_d_eq_b_not_today_no_fetch():
    # cache last == 10-23 (past, not today); request ends 10-23 -> no fetch
    out = run_sessions("2026-10-19", "2026-10-23", None, "2026-10-19", "2026-10-23")
    assert out == [], out
    assert run_refresh("2026-10-19", "2026-10-23", None, "2026-10-19", "2026-10-23") is False


def test_d_gt_b_ends_on_weekend_fetches_intermediate_only():
    # cache [10-19,10-23]; request to 10-25 (Sat, not in calendar).
    # 10-24/10-25 excluded by enumeration; nothing after cache_last in window -> []
    out = run_sessions("2026-10-19", "2026-10-23", None, "2026-10-19", "2026-10-25")
    assert out == [], out
    assert run_refresh("2026-10-19", "2026-10-23", None, "2026-10-19", "2026-10-25") is False


def test_d_gt_b_today_trading_day_fetch_regardless_of_open():
    # cache [10-19,10-23]; request to today 10-26 (trading day) -> fetch today
    # rule 5 says refresh regardless of open/closed (NOW is after open here).
    out = run_sessions("2026-10-19", "2026-10-23", datetime(2026, 10, 23, 18, 0),
                       "2026-10-19", "2026-10-26")
    assert out == ["2026-10-26"], out
    assert run_refresh("2026-10-19", "2026-10-23", datetime(2026, 10, 23, 18, 0),
                       "2026-10-19", "2026-10-26") is True


def test_backfill_and_append_combined():
    # cache [10-21,10-23]; request [10-19,10-27] -> 10-19,10-20 (backfill) + 10-26,10-27 (append)
    out = run_sessions("2026-10-21", "2026-10-23", None, "2026-10-19", "2026-10-27")
    assert out == ["2026-10-19", "2026-10-20", "2026-10-26", "2026-10-27"], out


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
