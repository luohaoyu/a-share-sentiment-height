"""Cache refresh decision logic for the unified per-day incremental bootstrap.

This module is the single source of truth for *whether* and *which* trading
sessions a symbol's cache must be (re)fetched for a given request window. It is
deliberately:

* **pure** — no network, no AkShare, no filesystem;
* **dependency-free** — only the standard library;
* **injectable** — the calendar and the "session open" gate are passed in as
  callables so the logic can be unit-tested deterministically.

It encodes the cache-refresh decision tree confirmed with the user:

    cache covers [A, B]   (A = first cached session, B = last cached session)
    request wants [C, D]

    1. empty cache                        -> refresh (fetch whole window)
    2. C < A                              -> refresh (backfill earlier sessions)
    3. D < B                              -> NO refresh (request fully inside)
    4. D == B:
         - not today or not a trading day -> NO refresh
         - today & trading day:
             last_update >= session_open(B) -> NO refresh (today's bar present)
             else                            -> refresh (today's bar stale)
    5. D > B:
         - D not a trading day            -> NO refresh
         - D is a trading day             -> refresh (open or not)

The calendar passed in already contains *only trading days* truncated at the
observation date, so "D not a trading day" (rule 5) is satisfied automatically
when we enumerate calendar entries: a non-trading date never appears, and future
sessions are absent because the calendar stops at ``as_of``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Optional

# A Beijing session opens at 09:30 CST. This is the "open time" gate used by the
# D == B (today) branch per the agreed rule: if the cache was last written at or
# after the session open, today's bar is considered already captured.
DEFAULT_SESSION_OPEN_HOUR = 9
DEFAULT_SESSION_OPEN_MINUTE = 30


def _as_date(iso: str) -> date:
    return date.fromisoformat(iso)


def _is_same_day(iso: str, now: datetime) -> bool:
    return _as_date(iso) == now.date()


def session_open_cst(iso: str) -> datetime:
    """Default session-open timestamp (09:30 CST, timezone-naive local time).

    Callers needing a different gate (e.g. 15:00 close) can pass their own
    callable instead of this default.
    """
    d = _as_date(iso)
    return datetime(d.year, d.month, d.day, DEFAULT_SESSION_OPEN_HOUR, DEFAULT_SESSION_OPEN_MINUTE)


def _default_is_trading_day(iso: str, calendar: list[str]) -> bool:
    return iso in set(calendar)


def sessions_to_fetch(
    cache_first: Optional[str],
    cache_last: Optional[str],
    last_update: Optional[datetime],
    req_first: str,
    req_last: str,
    now: datetime,
    calendar: list[str],
    is_trading_day: Optional[Callable[[str], bool]] = None,
    session_open: Optional[Callable[[str], datetime]] = None,
) -> list[str]:
    """Return the sorted trading sessions that must be fetched for this symbol.

    This is the authoritative implementation; :func:`needs_refresh` is a thin
    boolean view over it so the two can never disagree.

    Parameters
    ----------
    cache_first, cache_last:
        Inclusive first/last cached trading sessions, or ``None`` if the cache is
        empty (never fetched).
    last_update:
        Timestamp the cache was last written, or ``None`` if unknown.
    req_first, req_last:
        Inclusive first/last sessions the current run requests.
    now:
        "Current" time (injected for determinism).
    calendar:
        Ascending list of *trading* sessions truncated at the observation date.
    is_trading_day, session_open:
        Injectable predicates; default to membership in ``calendar`` and
        :func:`session_open_cst`.
    """
    is_trading_day = is_trading_day or (lambda d: _default_is_trading_day(d, calendar))
    session_open = session_open or session_open_cst

    # Rule 1: empty cache -> fetch the whole requested window.
    if cache_first is None or cache_last is None:
        return sorted(d for d in calendar if req_first <= d <= req_last)

    result: list[str] = []
    for d in calendar:
        if not (req_first <= d <= req_last):
            continue
        if cache_first <= d <= cache_last:
            # Already covered — but the "stale today" branch (rule 4) may still
            # require re-fetching today's bar.
            if (
                d == cache_last
                and req_last == cache_last
                and is_trading_day(cache_last)
                and _is_same_day(cache_last, now)
                and (last_update is None or last_update < session_open(cache_last))
            ):
                result.append(d)
            continue
        if d > cache_last:
            # Rule 5: append later sessions. Everything in ``calendar`` is a
            # trading day, so the "not a trading day -> no refresh" sub-rule is
            # satisfied by enumeration alone. Future sessions are absent from the
            # truncated calendar, so they are never fetched here.
            if is_trading_day(d):
                result.append(d)
        else:
            # Rule 2: backfill earlier sessions (all calendar entries are trading
            # days).
            result.append(d)
    return sorted(result)


def needs_refresh(
    cache_first: Optional[str],
    cache_last: Optional[str],
    last_update: Optional[datetime],
    req_first: str,
    req_last: str,
    now: datetime,
    calendar: list[str],
    is_trading_day: Optional[Callable[[str], bool]] = None,
    session_open: Optional[Callable[[str], datetime]] = None,
) -> bool:
    """Boolean view of :func:`sessions_to_fetch` — does this symbol need a refresh?"""
    return bool(
        sessions_to_fetch(
            cache_first,
            cache_last,
            last_update,
            req_first,
            req_last,
            now,
            calendar,
            is_trading_day,
            session_open,
        )
    )
