from datetime import UTC, datetime

import pytest

from really.pace import DEFAULT_WPM, NEEDED, Pace, Reading, estimate, middle, reading_minutes

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def read(wpm: float, words: int = 1000) -> Reading:
    return Reading(words, round(words * 60 / wpm), NOW)


def test_a_reading_knows_its_speed():
    assert Reading(2300, 600, NOW).wpm == 230
    assert Reading(2300, 0, NOW).wpm == 0


@pytest.mark.parametrize(
    ("reading", "plausible"),
    [
        (Reading(2300, 600, NOW), True),  # ten minutes for a ten-minute read
        (Reading(2300, 30, NOW), False),  # archived after a glance
        (Reading(2300, 3 * 3600, NOW), False),  # opened, then read after lunch
        (Reading(2300, 0, NOW), False),
        (Reading(2300, -40, NOW), False),  # the clock went backwards
        (Reading(250, 60, NOW), False),  # too short to time
        (Reading(300, 60, NOW), True),
        (read(80), True),
        (read(79), False),
        (read(600), True),
        (read(610), False),
    ],
)
def test_only_what_could_have_been_reading_is_plausible(reading, plausible):
    assert reading.plausible is plausible


def test_the_default_stands_until_there_are_enough_reads():
    assert estimate([]) == Pace(DEFAULT_WPM, 0, 0)
    few = [read(400)] * (NEEDED - 1)
    assert estimate(few) == Pace(DEFAULT_WPM, NEEDED - 1, 0)
    assert not estimate(few).measured
    assert estimate([read(400)] * NEEDED).measured


def test_the_fastest_and_slowest_are_set_aside():
    readings = [read(90), read(240), read(250), read(260), read(590)]
    pace = estimate(readings)
    assert pace == Pace(250, readings=5, counted=3)
    assert [round(reading.wpm) for reading in middle(readings)] == [240, 250, 260]


def test_what_is_set_aside_does_not_move_the_estimate():
    steady = [read(240), read(250), read(260)]
    assert estimate([read(245), *steady, read(255)]).wpm == 250
    assert estimate([read(80), *steady, read(600)]).wpm == 250


def test_more_is_set_aside_as_reads_accumulate():
    assert estimate([read(200 + n) for n in range(10)]).counted == 6
    assert estimate([read(200 + n) for n in range(20)]).counted == 12
    assert estimate([read(200 + n) for n in range(50)]).counted == 30


def test_one_slow_stretch_among_many_reads_is_absorbed():
    """A fifth of your reads can be off, in either direction, before the estimate notices."""
    honest = [read(250)] * 8
    assert estimate([*honest, read(85), read(90)]).wpm == 250
    assert estimate([*honest, read(580), read(590)]).wpm == 250


def test_long_reads_count_for_more_than_short_ones():
    readings = [
        read(100),
        Reading(300, 90, NOW),  # 200 words a minute, but over barely a page
        Reading(3000, 600, NOW),  # 300, sustained
        Reading(3000, 600, NOW),
        read(500),
    ]
    assert estimate(readings).wpm == 293  # nearer 300 than the 267 of averaging the speeds


def test_reading_minutes():
    assert reading_minutes(0) == 0
    assert reading_minutes(40) == 1
    assert reading_minutes(2300) == 10
    assert reading_minutes(2300, wpm=460) == 5
    assert reading_minutes(2300, wpm=115) == 20
