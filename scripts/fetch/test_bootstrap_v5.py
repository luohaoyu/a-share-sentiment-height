#!/usr/bin/env python3
"""Offline contract tests for the v5 unified bootstrap (qfq-only, per-day incremental).

The network layer (AkShare) is mocked with a minimal DataFrame-compatible object so
the tests run without akshare/pandas and without any network. We exercise the four
behaviours the v5 redesign must guarantee:

  1. cache fully covers the window  -> no network, status "cached"
  2. cache partially covers          -> only the missing sessions are fetched
  3. empty cache                     -> the whole requested window is fetched
  4. a source fails                  -> the task is re-routed to another source
  5. every source fails              -> the symbol lands in dead_letter with tried_sources
  6. a 北交所 (BJ) stock             -> tencent is never contacted
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("fa_v5_test", HERE / "fetch_akshare_data.py")
fa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fa)

# trading sessions used by the tests (all in the past relative to the run date so
# the "is today's bar captured" gate never triggers a refetch).
SESSIONS = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]


class FakeFrame:
    """Minimal pandas-substitute: enough of the API frame_ohlc_rows touches."""

    def __init__(self, columns, rows):
        self.columns = list(columns)
        self._rows = rows
        self.empty = len(rows) == 0

    def iterrows(self):
        for i, row in enumerate(self._rows):
            yield i, row


class FakeAk:
    """Mock AkShare. Records every adapter call and can be told which sources fail."""

    def __init__(self, fail=None, rows_for=None):
        self.calls = []  # (source, symbol, start_date, end_date, adjust)
        self.fail = set(fail or [])
        self.rows_for = rows_for or (lambda s: 100.0 + SESSIONS.index(s))

    def _frame(self, start_date, end_date):
        rows = []
        for s in SESSIONS:
            if start_date <= s.replace("-", "") <= end_date:
                base = self.rows_for(s)
                rows.append({
                    "date": s,
                    "open": base,
                    "high": base + 1.0,
                    "low": base - 1.0,
                    "close": base + 0.5,
                })
        return FakeFrame(["date", "open", "high", "low", "close"], rows)

    def _hit(self, source, symbol, start_date, end_date, adjust):
        self.calls.append((source, symbol, start_date, end_date, adjust))
        if source in self.fail:
            raise RuntimeError(f"{source} boom")
        return self._frame(start_date, end_date)

    def stock_zh_a_daily(self, symbol, start_date, end_date, adjust):
        return self._hit("sina", symbol, start_date, end_date, adjust)

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust, timeout):
        return self._hit("eastmoney", symbol, start_date, end_date, adjust)

    def stock_zh_a_hist_tx(self, symbol, start_date, end_date, adjust, timeout):
        return self._hit("tencent", symbol, start_date, end_date, adjust)


def make_stock(symbol, ts_code, exchange):
    return {"symbol": symbol, "ts_code": ts_code, "exchange": exchange, "list_date": "2010-01-04"}


def write_store(out_dir, ts_code, sessions_subset, last_update="2026-10-22T09:00:00"):
    rows = []
    for i, s in enumerate(sessions_subset):
        base = 100.0 + i
        rows.append({
            "trade_date": s,
            "open": base,
            "high": base + 1.0,
            "low": base - 1.0,
            "close": base + 0.5,
        })
    store = fa.prices_v4.build_store(ts_code, "2010-01-04", rows, "akshare_qfq", last_update)
    fa.prices_v4.write_store(Path(out_dir), store)


def run_bootstrap(out_dir, ak, master, sessions, picker=None, enabled=None, weights=None, max_attempts=3):
    if picker is not None:
        fa.pick_weighted_source = picker
    return fa.bootstrap(
        ak, Path(out_dir), master, sessions,
        retries=1, min_interval=0.0, selected_symbols=set(), workers=1,
        enabled=enabled or {"sina": True, "eastmoney": True, "tencent": True},
        weights=weights or {"sina": 5, "eastmoney": 3, "tencent": 2},
        max_attempts=max_attempts,
    )


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_cache_skip():
    print("[1] cache fully covers window -> skip, no network")
    with tempfile.TemporaryDirectory() as d:
        write_store(d, "000001.SZ", SESSIONS)
        ak = FakeAk()
        master = [make_stock("000001", "000001.SZ", "SZ")]
        manifest, dead = run_bootstrap(d, ak, master, SESSIONS)
        check("no network called", ak.calls == [], str(ak.calls))
        check("single manifest entry", len(manifest) == 1, str(manifest))
        check("status ok", manifest and manifest[0]["status"] == "ok", str(manifest))
        check("history_source == cache", manifest and manifest[0].get("history_source") == "cache", str(manifest))
        check("flag cached True", manifest and manifest[0].get("cached") is True, str(manifest))
        check("no dead letter", dead == [], str(dead))


def test_requested_coverage_skips_late_listing_empty_prefix():
    print("[1b] requested coverage exceeds observed K-lines -> skip, no network")
    with tempfile.TemporaryDirectory() as d:
        # Simulate a newly listed stock: the requested window starts on 10-20 but
        # its first observable K-line is 10-22. Persisted requested coverage must
        # prevent 10-20/10-21 from being fetched forever.
        write_store(d, "000001.SZ", SESSIONS[2:])
        store = fa.prices_v4.read_store(Path(d), "000001.SZ")
        store["requested_first"] = SESSIONS[0]
        store["requested_last"] = SESSIONS[-1]
        fa.prices_v4.write_store(Path(d), store)
        ak = FakeAk()
        master = [make_stock("000001", "000001.SZ", "SZ")]
        manifest, dead = run_bootstrap(d, ak, master, SESSIONS)
        check("late-listing prefix does not re-fetch", ak.calls == [], str(ak.calls))
        check("late-listing cache hit succeeds", manifest and manifest[0].get("cached") is True, str(manifest))
        check("late-listing no dead letter", dead == [], str(dead))


def test_per_day_incremental():
    print("[2] cache covers first three -> only the missing session fetched")
    with tempfile.TemporaryDirectory() as d:
        write_store(d, "000001.SZ", SESSIONS[:3])
        ak = FakeAk()
        master = [make_stock("000001", "000001.SZ", "SZ")]
        manifest, dead = run_bootstrap(d, ak, master, SESSIONS)
        check("exactly one network call", len(ak.calls) == 1, str(ak.calls))
        if ak.calls:
            src, sym, sd, ed, adj = ak.calls[0]
            check("request window == anchor plus missing day", (sd, ed) == ("20261022", "20261023"), str(ak.calls))
            check("adjust == qfq", adj == "qfq", str(ak.calls))
        store = fa.prices_v4.read_store(Path(d), "000001.SZ")
        first, last = fa.prices_v4.cache_range(store)
        check("store now covers full window", (first, last) == (SESSIONS[0], SESSIONS[-1]), f"{first},{last}")
        check("no dead letter", dead == [], str(dead))


def test_empty_cache_full_fetch():
    print("[3] empty cache -> whole window fetched in one qfq request")
    with tempfile.TemporaryDirectory() as d:
        ak = FakeAk()
        master = [make_stock("000001", "000001.SZ", "SZ")]
        manifest, dead = run_bootstrap(d, ak, master, SESSIONS)
        check("exactly one network call", len(ak.calls) == 1, str(ak.calls))
        if ak.calls:
            src, sym, sd, ed, adj = ak.calls[0]
            check("request window == whole window", (sd, ed) == ("20261020", "20261023"), str(ak.calls))
            check("adjust == qfq", adj == "qfq", str(ak.calls))
        store = fa.prices_v4.read_store(Path(d), "000001.SZ")
        first, last = fa.prices_v4.cache_range(store)
        check("store covers full window", (first, last) == (SESSIONS[0], SESSIONS[-1]), f"{first},{last}")
        check("no dead letter", dead == [], str(dead))


def test_cross_source_reroute():
    print("[4] primary source fails -> re-route to another source succeeds")
    with tempfile.TemporaryDirectory() as d:
        ak = FakeAk(fail={"sina"})
        master = [make_stock("000001", "000001.SZ", "SZ")]
        seq = iter(["sina", "eastmoney"])
        picker = lambda stock, enabled, weights: next(seq)  # noqa: E731
        manifest, dead = run_bootstrap(d, ak, master, SESSIONS[:1], picker=picker)
        check("symbol succeeded after reroute", manifest and manifest[0]["status"] == "ok", str(manifest))
        check("tried both sources", manifest and set(manifest[0].get("tried_sources", [])) >= {"sina", "eastmoney"}, str(manifest))
        check("no dead letter", dead == [], str(dead))


def test_dead_letter():
    print("[5] all sources fail -> dead_letter with tried_sources")
    with tempfile.TemporaryDirectory() as d:
        ak = FakeAk(fail={"sina", "eastmoney", "tencent"})
        master = [make_stock("000001", "000001.SZ", "SZ")]
        picks = ["sina", "eastmoney", "tencent"]
        idx = {"n": 0}

        def picker(stock, enabled, weights):
            s = picks[idx["n"] % len(picks)]
            idx["n"] += 1
            return s

        manifest, dead = run_bootstrap(d, ak, master, SESSIONS[:1], picker=picker, max_attempts=3)
        check("no manifest entry", manifest == [], str(manifest))
        check("one dead letter", len(dead) == 1, str(dead))
        if dead:
            check("dead letter tried 3 sources", set(dead[0].get("tried_sources", [])) >= {"sina", "eastmoney", "tencent"}, str(dead))
            check("dead letter has reasons", len(dead[0].get("reasons", [])) == 3, str(dead))


def test_bj_excludes_tencent():
    print("[6] BJ stock -> tencent never contacted")
    with tempfile.TemporaryDirectory() as d:
        ak = FakeAk()
        master = [make_stock("889001", "889001.BJ", "BJ")]
        # force the picker to *try* tencent first; fetch_qfq_frame must skip it.
        seq = iter(["tencent", "sina"])
        picker = lambda stock, enabled, weights: next(seq)  # noqa: E731
        manifest, dead = run_bootstrap(d, ak, master, SESSIONS[:1], picker=picker)
        check("tencent never called", all(c[0] != "tencent" for c in ak.calls), str(ak.calls))
        check("sina used instead", any(c[0] == "sina" for c in ak.calls), str(ak.calls))
        check("symbol succeeded", manifest and manifest[0]["status"] == "ok", str(manifest))


if __name__ == "__main__":
    test_cache_skip()
    test_requested_coverage_skips_late_listing_empty_prefix()
    test_per_day_incremental()
    test_empty_cache_full_fetch()
    test_cross_source_reroute()
    test_dead_letter()
    test_bj_excludes_tencent()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
