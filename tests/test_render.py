import io
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console

from really import render
from really.extract import Article
from really.pace import Pace, Reading, estimate
from really.store import MARK, UNMARK, Hit, State

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


def test_a_receipt_shows_what_something_was_called_and_is_called_now(item):
    out = console()
    render.receipt(out, "Archived", replace(item, state=State.ARCHIVED, number=3), was="7")
    assert "Archived #7 → #c The Bitter Lesson" in printed(out)
    out = console()
    render.receipt(out, "Requeued", replace(item, number=12), was="c")
    assert "Requeued #c → #12 The Bitter Lesson" in printed(out)


@pytest.mark.parametrize(
    ("state", "moves", "told"),
    [
        (State.QUEUED, [(4, 3)], "Queue renumbered: #4 is now #3."),
        (State.QUEUED, [(4, 3), (5, 4), (6, 5)], "Queue renumbered: #4 to #6 are now #3 to #5."),
        (
            State.QUEUED,
            [(6, 4), (2, 1), (5, 3)],
            "Queue renumbered: #2 is now #1; #5 to #6 are now #3 to #4.",
        ),
        (State.ARCHIVED, [(4, 3), (5, 4)], "Archive relettered: #d to #e are now #c to #d."),
        (State.ARCHIVED, [(27, 26)], "Archive relettered: #aa is now #z."),
        (
            State.QUEUED,
            [(n * 2, n) for n in range(1, 8)],
            "Queue renumbered: 7 others have new numbers.",
        ),
        (
            State.ARCHIVED,
            [(n * 2, n) for n in range(1, 8)],
            "Archive relettered: 7 others have new letters.",
        ),
    ],
)
def test_renumbered(state, moves, told):
    out = console()
    render.renumbered(out, state, moves)
    assert printed(out).strip() == told


def test_nothing_is_said_when_nothing_moved():
    out = console()
    render.renumbered(out, State.QUEUED, [])
    assert printed(out) == ""


def test_details_give_dates_as_well_as_ages(item):
    out = console()
    seen = replace(
        item,
        added_at=NOW - timedelta(days=40),
        opened_at=NOW - timedelta(days=2),
        fetched_at=NOW - timedelta(days=40),
    )
    render.details(out, seen, now=NOW)
    rows = {
        line.split()[0]: " ".join(line.split()[1:]) for line in printed(out).splitlines()[-5:-1]
    }
    assert rows["Saved"] == "copy 1,150 words · fetched 5w ago"
    # The date is shown as the reader's own clock had it, so only its shape is checked here.
    dated = r"\d{1,2} \w+ 2026, \d{2}:\d{2} · "
    assert re.fullmatch(dated + "5w ago", rows["Added"])
    assert re.fullmatch(dated + "2d ago", rows["Opened"])
    assert "Archived" not in rows


def test_details_of_an_archived_item(item):
    out = console()
    kept = replace(item, state=State.ARCHIVED, number=3, archived_at=NOW - timedelta(hours=5))
    render.details(out, kept, now=NOW)
    text = printed(out)
    assert "#c" in text
    assert re.search(r"Archived +\d{1,2} \w+ 2026, \d{2}:\d{2} · 5h ago", text)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (3 * 86400, "3 days"),
        (86400, "1 day"),
        (14 * 86400, "2 weeks"),
        (604800, "1 week"),
        (12 * 3600, "12 hours"),
        (3600, "1 hour"),
        (36 * 3600, "36 hours"),
        (5400, "1.5 hours"),
    ],
)
def test_span(seconds, text):
    assert render.span(seconds) == text


@pytest.mark.parametrize(
    ("until", "text"),
    [
        (timedelta(days=2, hours=23), "2d left"),
        (timedelta(days=1), "1d left"),
        (timedelta(hours=23, minutes=59), "23h left"),
        (timedelta(minutes=40), "40m left"),
        (timedelta(seconds=20), "1m left"),
        (timedelta(0), "due"),
        (timedelta(days=-4), "due"),
    ],
)
def test_left(until, text):
    assert render.left(NOW + until, NOW) == text


def test_a_row_says_how_long_something_has_before_it_expires(item):
    out = console()
    waiting = replace(item, added_at=NOW - timedelta(days=2), expires_at=NOW + timedelta(days=1))
    render.listing(out, [waiting, replace(item, added_at=NOW - timedelta(days=2))], now=NOW)
    first, second = printed(out).splitlines()[-2:]
    assert first.rstrip().endswith("2d · 1d left")
    assert second.rstrip().endswith("2d")


def test_expiry_rules_are_listed_with_what_waits_under_them(item):
    out = console()
    rules = {"example.com": 3 * 86400, "news.example.org": 12 * 3600}
    waiting = [
        replace(item, expires_at=NOW + timedelta(days=2)),
        replace(item, expires_at=NOW + timedelta(hours=5)),
    ]
    render.expiries(out, rules, waiting, now=NOW)
    lines = [" ".join(line.split()) for line in printed(out).splitlines()]
    assert "example.com after 3 days 2 waiting, the next has 5h left" in lines
    assert "news.example.org after 12 hours" in lines
