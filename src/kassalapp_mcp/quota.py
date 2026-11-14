"""Counting the calls to Kassalapp, and keeping within its free limit.

Kassalapp's free plan allows 60 calls a minute per account, and each answer
says how many are left. No daily cap is documented, so the calls are counted
per day too: if one ever appears, the count shows it. Kassalapp's headers are
kept as they came, so a new limit header shows itself without a code change.

Nothing here holds a search word, a barcode or a product name. Those are the
user's shopping, so a call is written down by its endpoint's template alone.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import math
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from kassalapp_mcp.source import LIMIT_PER_MINUTE, SlowDownError

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    import httpx

log = logging.getLogger(__name__)

MINUTE = dt.timedelta(minutes=1)
#: Warn when this share of the minute's calls, or fewer, is left.
WARN_SHARE = 0.2
#: Warn when our own count in the last minute reaches this many calls.
WARN_OWN = 48
#: How many days of totals are kept.
KEEP_DAYS = 90
_HEADER = "x-ratelimit-"


class Quota:
    """What Kassalapp's headers and our own count say about the limit."""

    def __init__(
        self, state_dir: pathlib.Path | None, now: Callable[[], dt.datetime], keep_lines: int = 10_000
    ) -> None:
        """Keep the usage files in ``state_dir``, or nowhere when it is None."""
        self._dir = state_dir
        self._now = now
        self._keep_lines = keep_lines
        self._calls: collections.deque[dt.datetime] = collections.deque()
        self._headers: dict[str, str] = {}
        self._seen_at: dt.datetime | None = None
        self._until: dt.datetime | None = None
        self._warned_at: dt.datetime | None = None
        self._days: dict[str, dict[str, int]] = self._load("usage-days.json")
        self._lines = len(self._read("usage.jsonl").splitlines())
        latest = self._load("status.json")
        self._headers = dict(latest.get("headers") or {})
        self._seen_at = _when(latest.get("seen_at"))
        self._until = _when(latest.get("backing_off_until"))

    def before_call(self) -> None:
        """Refuse a call while Kassalapp has asked us to wait, or our own count is at the limit."""
        now = self._now()
        self._forget(now)
        if len(self._calls) >= LIMIT_PER_MINUTE:
            self._until = max(self._until or now, self._calls[0] + MINUTE)
        # RULE: a 429 stops calls until retry after passes
        if self._until is not None and now < self._until:
            raise SlowDownError(math.ceil((self._until - now).total_seconds()))

    def after_call(self, endpoint: str, response: httpx.Response) -> None:
        """Record one call, by its endpoint's template only."""
        now = self._now()
        self._calls.append(now)
        self._headers = {k.lower(): v for k, v in response.headers.items() if k.lower().startswith(_HEADER)}
        self._seen_at = now
        wait = response.headers.get("retry-after", "")
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            self._count("429s")
            self._until = now + dt.timedelta(seconds=int(wait)) if wait.isdigit() else _next_minute(now)
        elif self._number("remaining") == 0:
            self._until = now + dt.timedelta(seconds=int(wait)) if wait.isdigit() else _next_minute(now)
        self._count("calls")
        line = {
            "at": now.isoformat(),
            "endpoint": endpoint,
            "status": response.status_code,
            "remaining": self._number("remaining"),
            "from_cache": False,
        }
        self._append(json.dumps(line))
        self._warn(now)
        self._save()

    def cache_hit(self) -> None:
        """Count a question the cache answered without a call."""
        self._count("cache_hits")
        self._save()

    def status(self) -> dict[str, Any]:
        """The quota as the plugin's status shows it."""
        now = self._now()
        self._forget(now)
        today = self._days.get(now.date().isoformat(), {})
        backing = self._until is not None and now < self._until
        return {
            "limit_per_min": self._number("limit"),
            "remaining": self._number("remaining"),
            "seen_at": self._seen_at.isoformat() if self._seen_at else None,
            "calls_last_min": len(self._calls),
            "calls_today": today.get("calls", 0),
            "cache_hits_today": today.get("cache_hits", 0),
            "429s_today": today.get("429s", 0),
            "level": "backing_off" if backing else ("warn" if self._low() else "ok"),
            "backing_off_until": self._until.isoformat() if backing and self._until else None,
            "headers": dict(self._headers),
        }

    def _low(self) -> bool:
        remaining = self._number("remaining")
        limit = self._number("limit") or LIMIT_PER_MINUTE
        # RULE: the level warns at twelve remaining
        by_header = remaining is not None and remaining <= limit * WARN_SHARE
        return by_header or len(self._calls) >= WARN_OWN

    def _warn(self, now: dt.datetime) -> None:
        if not self._low() or (self._warned_at and now - self._warned_at < MINUTE):
            return
        self._warned_at = now
        log.warning(
            "Kassalapp quota is low: %s calls left this minute, %d of ours in the last minute",
            self._number("remaining"),
            len(self._calls),
        )

    def _number(self, name: str) -> int | None:
        value = self._headers.get(_HEADER + name, "")
        return int(value) if value.isdigit() else None

    def _forget(self, now: dt.datetime) -> None:
        while self._calls and now - self._calls[0] > MINUTE:
            self._calls.popleft()

    def _count(self, what: str) -> None:
        today = self._now().date()
        day = self._days.setdefault(today.isoformat(), {"calls": 0, "cache_hits": 0, "429s": 0})
        day[what] = day.get(what, 0) + 1
        oldest = today - dt.timedelta(days=KEEP_DAYS)
        self._days = {d: n for d, n in self._days.items() if dt.date.fromisoformat(d) >= oldest}

    def _append(self, line: str) -> None:
        if self._dir is None:
            return
        path = self._dir / "usage.jsonl"
        with path.open("a") as usage:
            usage.write(line + "\n")
        self._lines += 1
        if self._lines > self._keep_lines:
            newest = path.read_text().splitlines()[-(self._keep_lines // 2) :]
            path.write_text("".join(x + "\n" for x in newest))
            self._lines = len(newest)

    def _save(self) -> None:
        if self._dir is None:
            return
        (self._dir / "usage-days.json").write_text(json.dumps(self._days, indent=1, sort_keys=True))
        (self._dir / "status.json").write_text(json.dumps(self.status(), indent=1))

    def _read(self, name: str) -> str:
        if self._dir is None:
            return ""
        try:
            return (self._dir / name).read_text()
        except FileNotFoundError:
            return ""

    def _load(self, name: str) -> dict[str, Any]:
        try:
            loaded = json.loads(self._read(name) or "{}")
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}


def _next_minute(now: dt.datetime) -> dt.datetime:
    return now.replace(second=0, microsecond=0) + MINUTE


def _when(value: object) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if isinstance(value, str) else None
