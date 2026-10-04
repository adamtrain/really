import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console

from really import render
from really.extract import Article
from really.pace import Pace, Reading, estimate
from really.store import MARK, UNMARK, Hit

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def console(width: int = 100) -> Console:
    return Console(file=io.StringIO(), width=width, highlight=False, record=True)


def printed(target: Console) -> str:
    return target.export_text(clear=False)


@pytest.fixture
def item(store):
    article = Article(
        title="The Bitter Lesson",
        author="Rich Sutton",
        site="Incomplete Ideas",
        text="word " * 1150,
    )
    return store.add("https://example.com/bitter", article, fetched=True, tags=["ml"])


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=20), "now"),
        (timedelta(minutes=5), "5m"),
        (timedelta(hours=3), "3h"),
        (timedelta(days=2), "2d"),
        (timedelta(days=13), "13d"),
        (timedelta(days=21), "3w"),
        (timedelta(days=100), "3mo"),
        (timedelta(days=800), "2y"),
    ],
)
def test_ago(delta, expected):
    assert render.ago(NOW - delta, NOW) == expected


def test_duration():
    assert render.duration(45) == "45 min"
    assert render.duration(60) == "1 h"
    assert render.duration(105) == "1 h 45 min"


def test_took():
    assert render.took(45) == "45 s"
    assert render.took(89) == "89 s"
    assert render.took(372) == "6 min"
    assert render.took(3900) == "1 h 5 min"


def test_reading_time(item):
    assert render.reading_time(item) == "5 min"
    assert render.reading_time(replace(item, paywalled=True)) == "5+ min"
    assert render.reading_time(replace(item, words=0)) == ""


def test_byline(item):
    assert render.byline(item) == "Rich Sutton · Incomplete Ideas"
    assert render.byline(replace(item, author="")) == "Incomplete Ideas"
    assert render.byline(replace(item, author="", site="")) == "example.com"
    named = replace(item, author="Simon Willison", site="Simon Willison's Newsletter")
    assert render.byline(named) == "Simon Willison's Newsletter"
    crowd = replace(item, author="A One, B Two, C Three")
    assert render.byline(crowd) == "A One et al. · Incomplete Ideas"


def test_listing_shows_what_matters(item):
    out = console()
    render.listing(out, [item], now=item.added_at + timedelta(days=3))
    line = printed(out).splitlines()[-1]
    for part in (str(item.id), "The Bitter Lesson", "#ml", "Rich Sutton", "5 min", "3d"):
        assert part in line


def test_titles_are_not_treated_as_markup(item):
    out = console()
    tricky = replace(item, title="Arrays [bold]and[/] slices [/]", author="[red]x")
    render.listing(out, [tricky])
    render.receipt(out, "Added", tricky)
    out.print(render.card(tricky, 80))
    assert printed(out).count("Arrays [bold]and[/] slices [/]") == 3


def test_long_titles_are_cut_not_wrapped(item):
    out = console(60)
    render.listing(out, [replace(item, title="A very long title indeed " * 6)])
    assert len(printed(out).splitlines()) == 3  # a blank line, the header, one row
    assert "…" in printed(out)


def test_opened_and_stale_items_stand_out(item):
    out = console()
    opened = replace(item, opened_at=NOW)
    render.listing(out, [opened], now=NOW)
    assert "▸" in printed(out)
    assert render._age_style(replace(item, added_at=NOW - timedelta(days=45)), NOW) == render.STALE
    assert render._age_style(replace(item, added_at=NOW - timedelta(days=200)), NOW) == render.ERROR


def test_queue_summary(item):
    out = console()
    render.queue_summary(out, [item, replace(item, minutes=10)], now=item.added_at)
    assert "2 things to read · about 15 min · oldest added just now" in printed(out)
    empty = console()
    render.queue_summary(empty, [])
    assert "really add" in printed(empty)


def test_hits_mark_matches(item):
    out = console()
    hit = Hit(item, f"The {MARK}Bitter{UNMARK} Lesson", f"…a {MARK}bitter{UNMARK} pill…")
    render.hits(out, [hit], "bitter")
    text = printed(out)
    assert "The Bitter Lesson" in text and "…a bitter pill…" in text
    assert MARK not in text and UNMARK not in text
    assert "1 match for bitter" in text
    assert "Note" not in text


def test_hits_show_a_matching_note(item):
    out = console()
    hit = Hit(
        item, item.title, "It opens like this", note=f"Cited in the {MARK}scaling{UNMARK} debate"
    )
    render.hits(out, [hit, hit], "scaling")
    assert "Note  Cited in the scaling debate" in printed(out)
    assert "2 matches for scaling" in printed(out)


def test_no_hits():
    out = console()
    render.hits(out, [], "zebra")
    assert "Nothing matches zebra" in printed(out)


def test_card_mentions_a_paywall(item):
    out = console()
    out.print(render.card(replace(item, paywalled=True, note="Worth it"), 80))
    assert "only the free preview is saved" in printed(out)
    assert "Worth it" in printed(out)


def test_article_without_a_copy_says_how_to_get_one(item):
    out = console()
    render.article(out, item, "")
    assert f"really refresh {item.id}" in printed(out)


def test_timed_line_before_and_after_your_pace_is_known():
    reading = Reading(2300, 552, NOW)
    out = console()
    render.timed(out, reading, Pace(230, readings=2, counted=0))
    assert "Read in 9 min, at 250 words a minute." in printed(out)
    assert "2 of the 5 timed reads needed" in printed(out)
    out = console()
    render.timed(out, reading, Pace(243, readings=12, counted=8))
    assert "Your pace is 243, going by 12 timed reads." in printed(out)


def test_pace_view_before_anything_is_timed():
    out = console()
    render.pace(out, Pace(230, 0, 0), [])
    text = printed(out)
    assert "Reading times assume 230 words a minute for now" in text
    assert "0 of the 5 timed reads" in text
    assert "under 80 or over 600 words a minute" in " ".join(text.split())


def test_pace_view_shows_the_reads_and_which_were_set_aside():
    speeds = [250, 95, 240, 560, 260]
    readings = [
        Reading(2000, round(2000 * 60 / wpm), NOW - timedelta(days=n))
        for n, wpm in enumerate(speeds)
    ]
    out = console()
    render.pace(out, estimate(readings), readings, now=NOW)
    lines = printed(out).splitlines()
    assert "You read about 250 words a minute" in lines[1]
    assert "your last 5 timed reads, with the fastest and slowest set aside" in lines[2]
    rows = [line for line in lines if "2,000" in line]
    assert [row.split()[0] for row in rows] == ["now", "1d", "2d", "3d", "4d"]
    assert "slowest, set aside" in rows[1] and "fastest, set aside" in rows[3]
    assert "set aside" not in rows[0] + rows[2] + rows[4]


def test_pace_view_only_lists_the_latest():
    readings = [Reading(2000, 480, NOW - timedelta(days=n)) for n in range(30)]
    out = console()
    render.pace(out, estimate(readings), readings, now=NOW)
    assert "the 6 fastest and 6 slowest set aside" in printed(out)
    assert "and 18 before those" in printed(out)
