"""YouTube Data API quota accounting (11.4).

The corrections this module implements, from 11.4:

* the daily allowance is 10,000 units and one upload costs about 1,600, so a
  default project can publish roughly six videos a day - a scheduling fact, not
  an edge case;
* the allowance resets at midnight **Pacific time**, not 24 hours after the
  failure, so "retry in 24h" would waste most of a day or retry too early
  depending on when the failure happened. The retry queue targets the next PT
  midnight.

The unit costs are documented API figures, not measurements, so they are not
profile parameters; the daily allowance is, because a project whose quota
increase was granted has a different one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Documented Data API v3 costs.
COST_VIDEO_INSERT = 1600
COST_THUMBNAIL_SET = 50
COST_LIST = 1
COST_SEARCH = 100


def _pt_zone(name: str = "America/Los_Angeles"):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:                      # tzdata missing on some minimal images
        log.warning("timezone database unavailable; falling back to a fixed UTC-8 for the quota reset")
        return timezone(timedelta(hours=-8))


@dataclass
class QuotaState:
    pt_date: str
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


class QuotaLedger:
    """Tracks spend against the PT day and answers "can this upload run now?"."""

    def __init__(self, store, *, daily_limit: int = 10000, timezone_name: str = "America/Los_Angeles"):
        self.store = store
        self.daily_limit = daily_limit
        self.zone = _pt_zone(timezone_name)

    def pt_now(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(self.zone)

    def pt_date(self) -> str:
        return self.pt_now().strftime("%Y-%m-%d")

    def next_reset(self, *, after: datetime | None = None) -> datetime:
        """Midnight PT following ``after`` - what the retry queue schedules on."""
        now = (after or datetime.now(timezone.utc)).astimezone(self.zone)
        tomorrow = (now + timedelta(days=1)).date()
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=self.zone)

    def state(self) -> QuotaState:
        date = self.pt_date()
        return QuotaState(pt_date=date, used=self.store.quota_used(date), limit=self.daily_limit)

    def can_afford(self, units: int) -> bool:
        return self.state().remaining >= units

    def spend(self, units: int, reason: str) -> QuotaState:
        self.store.record_quota(self.pt_date(), units, reason)
        return self.state()

    def uploads_left_today(self) -> int:
        return self.state().remaining // COST_VIDEO_INSERT
