"""Work out how fast you read from how long things actually took you.

A read is timed from the moment you open something to the moment you archive or delete it.
Most of those timings are honest, but not all: you open a tab and come back to it after lunch,
or archive something after a glance. So a timing is only kept if it could have been someone
reading the piece start to finish, and the estimate sets aside the fastest and slowest of the
ones that are kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# A typical adult reading non-fiction. Used until there are enough timed reads to do better.
DEFAULT_WPM = 230

# Slower than this and you were away for part of it; faster and you didn't read it all.
SLOWEST_WPM = 80
FASTEST_WPM = 600
# Shorter than this, the seconds spent switching windows are too much of the total.
SHORTEST_WORDS = 300

NEEDED = 5  # timed reads before the estimate takes over from the default
RECENT = 50  # how many of the latest reads it looks at, so it follows a change of pace
TRIM = 0.2  # the share set aside at each end


@dataclass(frozen=True, slots=True)
class Reading:
    """One timed read."""

    words: int
    seconds: int
    finished_at: datetime

    @property
    def wpm(self) -> float:
        return self.words * 60 / self.seconds if self.seconds > 0 else 0.0

    @property
    def plausible(self) -> bool:
        """Whether this could be someone reading the whole piece in one sitting."""
        return self.words >= SHORTEST_WORDS and SLOWEST_WPM <= self.wpm <= FASTEST_WPM


@dataclass(frozen=True, slots=True)
class Pace:
    wpm: int  # what reading times are worked out from
    readings: int  # recent timed reads on record
    counted: int  # how many of them the estimate rests on; none while it's the default

    @property
    def measured(self) -> bool:
        return self.counted > 0


def middle(readings: list[Reading]) -> list[Reading]:
    """The readings left once the fastest and slowest are set aside. Slowest first."""
    cut = max(1, round(len(readings) * TRIM))
    return sorted(readings, key=lambda reading: reading.wpm)[cut:-cut]


def estimate(readings: list[Reading]) -> Pace:
    """Your pace: total words over total time, across the middle of your recent reads.

    Dividing the totals, rather than averaging each read's speed, lets a long read count for
    more than a short one, whose timing is the less reliable of the two.
    """
    if len(readings) < NEEDED:
        return Pace(DEFAULT_WPM, len(readings), 0)
    kept = middle(readings)
    words, seconds = sum(r.words for r in kept), sum(r.seconds for r in kept)
    return Pace(round(words * 60 / seconds), len(readings), len(kept))


def reading_minutes(words: int, wpm: int = DEFAULT_WPM) -> int:
    return max(1, round(words / wpm)) if words else 0
