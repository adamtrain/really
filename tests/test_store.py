import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from really.extract import Article
from really.pace import DEFAULT_WPM, RECENT, Pace, Reading
from really.store import (
    ENV_DB,
    LONGEST_CODE,
    MARK,
    MIGRATIONS,
    SCHEMA_VERSION,
    UNMARK,
    Ambiguous,
    NotFound,
    State,
    Store,
    StoreError,
    default_path,
    from_letters,
    letters,
    match_expression,
    normalize_tags,
)

LESSON = Article(
    title="The Bitter Lesson",
    author="Rich Sutton",
    site="Incomplete Ideas",
    summary="What 70 years of AI research teaches.",
    content="General **methods** that leverage [computation](https://example.com) win.",
    text="General methods that leverage computation win. Researchers keep relearning this.",
)
CAFE = Article(
    title="Notes from a Café",
    author="Sam Okafor",
    site="Fieldnotes",
    text="The café was running late, so I read about queues and promises instead.",
    content="The café was running late, so I read about queues and promises instead.",
)


def plain(marked: str) -> str:
    return marked.replace(MARK, "[").replace(UNMARK, "]")


@pytest.fixture
def lesson(store):
    return store.add("https://example.com/bitter", LESSON, fetched=True, tags=["ml"])


@pytest.fixture
def cafe(store, lesson):
    return store.add("https://fieldnotes.example/cafe", CAFE, fetched=True)


def test_add_and_get(store, lesson):
    assert store.get(lesson.id) == lesson
    assert lesson.title == "The Bitter Lesson"
    assert lesson.state is State.QUEUED
    assert lesson.tags == ("ml",)
    assert lesson.words == 10
    assert lesson.minutes == 1
    assert lesson.fetched_at is not None
    assert lesson.archived_at is None
    assert store.content(lesson.id) == LESSON.content
    assert store.by_url("https://example.com/bitter") == lesson
    assert store.get(999) is None


def test_a_bare_link_gets_a_stand_in_name(store):
    item = store.add("https://example.com/posts/on-reading-slowly")
    assert item.title == ""
    assert item.name == "On reading slowly"
    assert item.fetched_at is None
    assert item.minutes == 0


def test_urls_are_unique(store, lesson):
    with pytest.raises(sqlite3.IntegrityError):
        store.add(lesson.url)


def test_items_come_in_the_order_they_joined_and_filter(store):
    now = datetime.now(UTC)
    first = store.add("https://example.com/first", added_at=now, tags=["a"])
    older = store.add(
        "https://example.com/older", added_at=now - timedelta(days=9), tags=["a", "b"]
    )
    kept = store.archive(store.add("https://example.com/kept").id)
    assert store.items(State.QUEUED) == [first, older]  # by when they were queued, not dated
    assert store.items(State.ARCHIVED) == [kept]
    assert store.items() == [first, older, kept]  # the queue, then the archive
    assert store.items(tag="b") == [older]
    assert store.items(tag="#A") == [first, older]
    assert len(store.items()) == 3
    assert store.count(State.QUEUED) == 2


def test_archive_and_requeue(store, lesson):
    archived = store.archive(lesson.id)
    assert archived.archived and archived.archived_at is not None
    again = store.requeue(lesson.id)
    assert again.state is State.QUEUED and again.archived_at is None


def test_last_opened_is_the_most_recent_queued_one(store, lesson, cafe):
    assert store.last_opened() is None
    store._set(lesson.id, opened_at="2026-01-01T00:00:00+00:00")
    store._set(cafe.id, opened_at="2026-01-02T00:00:00+00:00")
    assert store.last_opened().id == cafe.id
    store.archive(cafe.id)
    assert store.last_opened().id == lesson.id


# ── Search ────────────────────────────────────────────────────────────────────


def test_search_finds_words_in_the_text(store, lesson, cafe):
    [hit] = store.search("computation")
    assert hit.item == lesson
    assert "leverage [computation] win" in plain(hit.snippet)


def test_search_stems_and_ignores_accents(store, cafe):
    assert [hit.item for hit in store.search("cafe")] == [cafe]
    assert [hit.item for hit in store.search("promise run")] == [cafe]


def test_search_needs_every_word(store, lesson, cafe):
    assert store.search("computation café") == []


def test_search_phrases_prefixes_and_fields(store, lesson, cafe):
    assert [hit.item for hit in store.search('"leverage computation"')] == [lesson]
    assert store.search('"computation leverage"') == []
    assert [hit.item for hit in store.search("research*")] == [lesson]
    assert [hit.item for hit in store.search("author:sutton")] == [lesson]
    assert store.search("author:computation") == []
    assert [hit.item for hit in store.search("tag:ml")] == [lesson]
    assert [hit.item for hit in store.search("site:fieldnotes queues")] == [cafe]


def test_title_matches_rank_first_and_are_marked(store, lesson, cafe):
    store.add("https://example.com/other", Article(title="Other", text="a bitter aftertaste"))
    first, second = store.search("bitter")
    assert first.item == lesson
    assert plain(first.title) == "The [Bitter] Lesson"
    assert plain(second.title) == "Other"


def test_a_match_in_your_note_shows_the_note(store, lesson):
    store.edit(lesson.id, note="Cited in the scaling debate")
    [hit] = store.search("scaling")
    assert plain(hit.note) == "Cited in the [scaling] debate"
    assert plain(hit.snippet).startswith("General methods")  # no match there: its opening
    [elsewhere] = store.search("computation")
    assert elsewhere.note == ""


def test_search_filters_by_state_and_tag(store, lesson, cafe):
    store.archive(cafe.id)
    both = "read OR general"  # no OR operator: these are just three words
    assert store.search(both) == []
    assert [hit.item.id for hit in store.search("the", State.ARCHIVED)] == [cafe.id]
    assert [hit.item.id for hit in store.search("the", State.QUEUED)] == [lesson.id]
    assert [hit.item.id for hit in store.search("the", tag="ml")] == [lesson.id]


def test_the_index_follows_edits_and_deletes(store, lesson):
    store.edit(lesson.id, title="A Sweeter Lesson")
    assert store.search("bitter") == []
    assert len(store.search("sweeter")) == 1
    store.set_tags(lesson.id, ["history"])
    assert store.search("tag:ml") == []
    store.delete(lesson.id)
    assert store.search("sweeter") == []
    assert store.db.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")


@pytest.mark.parametrize(
    "query", ['"', "(", "c++", "a AND", "NOT", "title:", "-", "*", "'; DROP TABLE items; --", ""]
)
def test_no_query_is_a_syntax_error(store, lesson, query):
    store.search(query)


@pytest.mark.parametrize(
    ("typed", "expression"),
    [
        ("bitter lesson", '"bitter" "lesson"'),
        ('"bitter lesson" sutton', '"bitter lesson" "sutton"'),
        ("comput*", '"comput"*'),
        ("author:sutton", 'author: "sutton"'),
        ('by:"rich sutton"', 'author: "rich sutton"'),
        ("tag:ml", 'tags: "ml"'),
        ("http://example.com", '"http://example.com"'),
        ('say "hi', '"say" """hi"'),
        ("— ! …", ""),
    ],
)
def test_match_expression(typed, expression):
    assert match_expression(typed) == expression


# ── Finding the item someone means ────────────────────────────────────────────


def test_resolve(store, lesson, cafe):
    assert store.resolve(str(lesson.id)) == lesson
    assert store.resolve(f"#{cafe.id}") == cafe
    assert store.resolve("bitter") == lesson
    assert store.resolve("CAFÉ") == cafe
    assert store.resolve("https://example.com/bitter?utm_source=x#top") == lesson
    assert store.resolve("fieldnotes.example") == cafe


def test_resolve_refuses_to_guess(store, lesson, cafe):
    with pytest.raises(Ambiguous) as e:
        store.resolve("example")
    assert e.value.matches == [lesson, cafe]
    with pytest.raises(NotFound):
        store.resolve("zebra")
    with pytest.raises(NotFound):
        store.resolve("999")
    with pytest.raises(NotFound):
        store.resolve("100%")  # a wildcard to SQL, but not to us


def test_resolve_can_be_limited_to_a_state(store, lesson, cafe):
    store.archive(cafe.id)
    assert store.resolve("example", State.QUEUED) == lesson
    with pytest.raises(NotFound, match="queue"):
        store.resolve("café", State.QUEUED)
    assert store.resolve("a", State.QUEUED).id == cafe.id  # its letter always finds it


# ── Changing things ───────────────────────────────────────────────────────────


def test_a_fresh_fetch_fills_blanks_and_replaces_the_copy(store):
    item = store.add("https://t.example/redirect?id=1")
    updated = store.set_article(item.id, LESSON, "https://example.com/bitter")
    assert updated.url == "https://example.com/bitter"
    assert updated.title == "The Bitter Lesson"
    assert updated.author == "Rich Sutton"
    assert updated.words == 10
    assert updated.fetched_at is not None
    assert store.content(item.id) == LESSON.content
    assert len(store.search("computation")) == 1


def test_a_fresh_fetch_keeps_your_corrections(store, lesson):
    store.edit(lesson.id, title="Sutton's Lesson")
    updated = store.set_article(lesson.id, CAFE)
    assert updated.title == "Sutton's Lesson"
    assert updated.author == "Rich Sutton"
    assert store.content(lesson.id) == CAFE.content


def test_an_empty_fetch_does_not_wipe_the_copy(store, lesson):
    updated = store.set_article(lesson.id, Article(title="Access denied"))
    assert updated.words == 10
    assert store.content(lesson.id) == LESSON.content


def test_a_fresh_fetch_does_not_trade_your_copy_for_a_lesser_one(store):
    whole = Article(title="Paid", text="word " * 400, content="word " * 400)
    item = store.add("https://example.com/paid", whole, fetched=True)
    preview = Article(text="word " * 250, content="word " * 250, paywalled=True)
    kept = store.set_article(item.id, preview)
    assert (kept.words, kept.paywalled) == (400, False)
    stub = Article(text="This post has been removed by its author.", content="Removed.")
    assert store.set_article(item.id, stub).words == 400
    trimmed = Article(text="word " * 320, content="word " * 320)  # say, footnotes not counted
    assert store.set_article(item.id, trimmed).words == 320


def test_a_preview_is_replaced_by_a_fresh_preview(store):
    preview = Article(text="word " * 150, content="word " * 150, paywalled=True)
    item = store.add("https://example.com/paid", preview, fetched=True)
    longer = Article(text="word " * 160, content="word " * 160, paywalled=True)
    assert store.set_article(item.id, longer).words == 160


def test_footnote_words_are_stored_but_not_counted(store):
    article = Article(text="body " * 100 + "note " * 30, content="x", footnote_words=30)
    item = store.add("https://example.com/noted", article, fetched=True)
    assert item.words == 100
    assert len(store.search("note")) == 1


def test_a_fetch_never_merges_two_items(store, lesson, cafe):
    assert store.set_article(cafe.id, CAFE, lesson.url).url == cafe.url


def test_set_text_replaces_a_preview(store):
    preview = Article(title="Paid", text="Just the start", content="Just the start", paywalled=True)
    item = store.add("https://example.com/paid", preview, fetched=True)
    assert item.paywalled
    whole = store.set_text(item.id, "The whole thing, start to finish, with the ending.")
    assert not whole.paywalled
    assert whole.words == 9
    assert whole.title == "Paid"
    assert len(store.search("ending")) == 1


def test_edit_rejects_unknown_fields(store, lesson):
    with pytest.raises(ValueError, match="words"):
        store.edit(lesson.id, words="5")


def test_tags_are_normalized():
    assert normalize_tags(["ML", "#Deep Learning", "ml", " ", "a, b"]) == (
        "ml",
        "deep-learning",
        "a",
        "b",
    )


def test_stats(store, lesson, cafe):
    store.add("https://example.com/unfetched", added_at=datetime.now(UTC) - timedelta(days=40))
    store.archive(cafe.id)
    numbers = store.stats()
    assert (numbers.queued, numbers.archived, numbers.unsized) == (2, 1, 1)
    assert numbers.queued_minutes == 1
    assert numbers.archived_words == cafe.words
    assert numbers.oldest.url == "https://example.com/unfetched"
    assert (numbers.added_lately, numbers.archived_lately) == (2, 1)
    assert ("ml", 1) in numbers.tags
    assert ("Fieldnotes", 1) in numbers.sites


# ── What things are called ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("number", "code"),
    [
        (1, "a"),
        (2, "b"),
        (26, "z"),
        (27, "aa"),
        (28, "ab"),
        (52, "az"),
        (53, "ba"),
        (702, "zz"),
        (703, "aaa"),
        (18278, "zzz"),
        (18279, "aaaa"),
    ],
)
def test_letters_count_like_spreadsheet_columns(number, code):
    assert letters(number) == code
    assert from_letters(code) == from_letters(code.upper()) == number


def test_letters_and_numbers_round_trip():
    assert all(from_letters(letters(n)) == n for n in range(1, 20_000))


def queue_of(store, count: int) -> list:
    return [
        store.add(f"https://example.com/{n}", Article(title=f"Thing {n}")) for n in range(count)
    ]


def places(store, state: State | None = None) -> list[str]:
    """Each item as "what it's called: which thing it is"."""
    return [f"{item.ref}: {item.title[-1]}" for item in store.items(state)]


def test_the_queue_is_numbered_from_one(store):
    queue_of(store, 3)
    assert places(store) == ["1: 0", "2: 1", "3: 2"]


def test_archiving_gives_the_next_letter_and_closes_up_the_queue(store):
    things = queue_of(store, 5)
    archived = store.archive(things[1].id)
    assert (archived.ref, archived.number, archived.id) == ("a", 1, things[1].id)
    assert store.archive(things[3].id).ref == "b"
    assert places(store) == ["1: 0", "2: 2", "3: 4", "a: 1", "b: 3"]
    assert store.archive(things[1].id).ref == "a"  # archiving it again changes nothing
    assert places(store) == ["1: 0", "2: 2", "3: 4", "a: 1", "b: 3"]


def test_deleting_closes_up_whichever_list_it_was_in(store):
    things = queue_of(store, 5)
    for thing in things[:3]:
        store.archive(thing.id)
    store.delete(things[0].id)
    assert places(store) == ["1: 3", "2: 4", "a: 1", "b: 2"]
    store.delete(things[3].id)
    assert places(store) == ["1: 4", "a: 1", "b: 2"]
    store.delete(things[3].id)  # already gone: nothing happens, and nothing moves
    assert places(store) == ["1: 4", "a: 1", "b: 2"]


def test_requeueing_joins_the_end_of_the_queue_and_closes_up_the_archive(store):
    things = queue_of(store, 4)
    for thing in things[:3]:
        store.archive(thing.id)
    back = store.requeue(things[0].id)
    assert (back.ref, back.state, back.archived_at) == ("2", State.QUEUED, None)
    assert places(store) == ["1: 3", "2: 0", "a: 1", "b: 2"]
    assert store.requeue(things[0].id).ref == "2"  # already queued: nothing changes


def test_a_new_item_takes_the_lowest_number_free(store):
    things = queue_of(store, 3)
    store.delete(things[0].id)
    store.archive(things[1].id)
    fresh = store.add("https://example.com/fresh", Article(title="Thing 9"))
    assert fresh.ref == "2"
    assert fresh.id == 4  # though what the database knows it by is never reused
    straight_in = store.add(
        "https://example.com/read", Article(title="Thing 8"), state=State.ARCHIVED
    )
    assert straight_in.ref == "b"


def test_numbers_and_letters_stay_gapless_through_anything(store):
    things = queue_of(store, 30)
    for step, thing in enumerate(things):
        if step % 3 == 0:
            store.archive(thing.id)
        elif step % 3 == 1:
            store.delete(thing.id)
        if step % 7 == 0:
            store.requeue(things[step // 2].id) if store.get(things[step // 2].id) else None
        for state in State:
            numbers = [item.number for item in store.items(state)]
            assert numbers == list(range(1, len(numbers) + 1))


def test_renumbering_leaves_the_search_index_alone(store, lesson, cafe):
    store.delete(lesson.id)
    moved = store.get(cafe.id)
    assert (moved.ref, moved.id) == ("1", cafe.id)
    assert [hit.item.ref for hit in store.search("café")] == ["1"]
    assert store.content(cafe.id) == CAFE.content
    assert store.db.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")


def test_resolve_by_number_and_by_letters(store, lesson, cafe):
    store.archive(lesson.id)
    assert store.resolve("1").id == cafe.id  # numbers are always the queue
    assert store.resolve("a").id == store.resolve("#A").id == lesson.id  # letters, the archive
    with pytest.raises(NotFound, match="no #2 in your queue"):
        store.resolve("2")
    with pytest.raises(NotFound, match="no #b in your archive"):
        store.resolve("b")


def test_letters_that_are_not_a_place_are_words_from_a_title(store, lesson, cafe):
    store.archive(lesson.id)
    assert store.resolve("bitter").id == lesson.id  # there is no archived item "bitter"
    assert store.resolve("notes").id == cafe.id
    endless = "z" * (LONGEST_CODE + 30)  # far too long to be anyone's place in an archive
    with pytest.raises(NotFound, match=f'Nothing matches "{endless}"'):
        store.resolve(endless)


def test_a_list_from_before_things_were_numbered_is_numbered(tmp_path):
    path = tmp_path / "really.db"
    old = sqlite3.connect(path)
    for script in MIGRATIONS[:2]:
        old.executescript(script)
    old.execute("PRAGMA user_version = 2")
    rows = [
        # id, state, added, archived: ids with gaps, as a list that has seen deletions has
        (2, "queued", "2026-03-01T00:00:00+00:00", None),
        (5, "archived", "2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"),
        (6, "queued", "2026-02-01T00:00:00+00:00", None),
        (9, "archived", "2026-04-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00"),
        (11, "queued", "2026-03-01T00:00:00+00:00", None),
    ]
    for id, state, added, archived in rows:
        old.execute(
            "INSERT INTO items (id, url, title, text, state, added_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, f"https://example.com/{id}", f"Was {id}", f"keepsake{id}", state, added, archived),
        )
    old.commit()
    old.close()
    with Store(path) as store:
        # The queue by when each was added, the archive by when each was archived.
        assert [(item.ref, item.id) for item in store.items()] == [
            ("1", 6),
            ("2", 2),
            ("3", 11),
            ("a", 9),
            ("b", 5),
        ]
        assert store.search("keepsake9")[0].item.ref == "a"
        assert store.add("https://example.com/new").ref == "4"
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    with Store(path) as store:  # and opening it again changes nothing
        assert [item.ref for item in store.items()] == ["1", "2", "3", "4", "a", "b"]


# ── Timing reads ──────────────────────────────────────────────────────────────


def opened(store, clock, words: int, minutes_ago: float, url="https://example.com/read", **more):
    """Something of this length that you opened this long ago."""
    article = Article(title="A Read", text="word " * words, content="word " * words, **more)
    item = store.mark_opened(store.add(url, article, fetched=True).id)
    clock.advance(minutes=minutes_ago)
    return item


def test_finishing_times_the_read(store, clock):
    item = opened(store, clock, 2300, minutes_ago=10)
    reading = store.finish(item.id)
    assert reading == Reading(2300, 600, clock.now)
    assert reading.wpm == 230
    assert store.readings() == [reading]


@pytest.mark.parametrize(
    ("words", "minutes_ago"),
    [
        (2300, 0.5),  # archived after a glance
        (2300, 180),  # opened, then read after lunch
        (2300, 60 * 24 * 3),  # opened on Monday, archived on Thursday
        (200, 1),  # too short to time
        (0, 5),  # no idea how long it is
    ],
)
def test_timings_that_could_not_be_reading_are_not_kept(store, clock, words, minutes_ago):
    item = opened(store, clock, words, minutes_ago)
    assert store.finish(item.id) is None
    assert store.readings() == []
    assert store.pace() == Pace(DEFAULT_WPM, 0, 0)


def test_only_something_you_opened_from_the_queue_is_timed(store, clock):
    unopened = store.add("https://example.com/a", Article(text="word " * 2300), fetched=True)
    clock.advance(minutes=10)
    assert store.finish(unopened.id) is None
    preview = opened(store, clock, 2300, 10, url="https://example.com/b", paywalled=True)
    assert store.finish(preview.id) is None
    kept = opened(store, clock, 2300, 10, url="https://example.com/c")
    store.archive(kept.id)
    assert store.finish(kept.id) is None
    assert store.readings() == []


def test_opening_again_restarts_the_clock(store, clock):
    item = opened(store, clock, 2300, minutes_ago=60 * 5)  # opened this morning, never read
    store.mark_opened(item.id)
    clock.advance(minutes=10)
    reading = store.finish(item.id)
    assert reading is not None and reading.seconds == 600


def test_readings_outlast_what_was_read(store, clock):
    item = opened(store, clock, 2300, 10)
    store.finish(item.id)
    store.delete(item.id)
    assert len(store.readings()) == 1


def test_reading_times_follow_your_pace(store):
    item = store.add("https://example.com/long", Article(text="word " * 2300), fetched=True)
    assert item.minutes == 10
    for _ in range(4):
        store.record(Reading(4600, 600, datetime.now(UTC)))
    assert not store.pace().measured
    assert store.get(item.id).minutes == 10
    store.record(Reading(4600, 600, datetime.now(UTC)))
    assert store.pace() == Pace(460, readings=5, counted=3)
    assert store.get(item.id).minutes == 5
    assert [found.minutes for found in store.items()] == [5]
    assert store.search("word")[0].item.minutes == 5
    numbers = store.stats()
    assert (numbers.queued_minutes, numbers.pace.wpm) == (5, 460)


def test_the_estimate_follows_a_change_of_pace(store):
    for _ in range(RECENT):
        store.record(Reading(1000, 600, datetime.now(UTC)))  # 100 words a minute
    assert store.pace().wpm == 100
    for _ in range(RECENT):
        store.record(Reading(4000, 600, datetime.now(UTC)))  # 400
    assert store.pace() == Pace(400, readings=RECENT, counted=30)


# ── Summaries ─────────────────────────────────────────────────────────────────


def test_a_summary_is_kept_with_its_item(store, lesson):
    assert store.tldr(lesson.id) == ""
    store.set_tldr(lesson.id, "General methods win.")
    assert store.tldr(lesson.id) == "General methods win."
    assert store.text(lesson.id) == LESSON.text
    assert store.tldr(999) == store.text(999) == ""


def test_a_summary_is_nowhere_but_where_it_is_asked_for(store, lesson):
    store.set_tldr(lesson.id, "A summary mentioning zeppelins.")
    assert store.get(lesson.id) == lesson  # the item itself is unchanged
    assert store.search("zeppelins") == []
    assert "zeppelins" not in store.content(lesson.id)


def test_a_summary_lasts_as_long_as_the_text_it_summarizes(store, lesson):
    store.set_tldr(lesson.id, "General methods win.")
    store.set_article(lesson.id, LESSON)  # fetched again, and nothing has changed
    assert store.tldr(lesson.id) == "General methods win."
    store.archive(lesson.id)
    store.edit(lesson.id, note="Read twice")
    assert store.tldr(lesson.id) == "General methods win."
    store.set_article(lesson.id, CAFE)  # the page says something else now
    assert store.tldr(lesson.id) == ""
    store.set_tldr(lesson.id, "About a café.")
    store.set_text(lesson.id, "A different text altogether, pasted in by hand.")
    assert store.tldr(lesson.id) == ""


def test_a_list_from_before_summaries_gains_a_place_for_them(tmp_path):
    path = tmp_path / "really.db"
    old = sqlite3.connect(path)
    for script in MIGRATIONS[:3]:
        old.executescript(script)
    old.execute("PRAGMA user_version = 3")
    old.execute(
        "INSERT INTO items (url, title, text, words, number, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "https://example.com/old",
            "From Before",
            "an heirloom",
            2,
            1,
            "2026-01-01T00:00:00+00:00",
        ),
    )
    old.commit()
    old.close()
    with Store(path) as store:
        [item] = store.items()
        assert (item.title, item.ref, store.tldr(item.id)) == ("From Before", "1", "")
        store.set_tldr(item.id, "An heirloom.")
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    with Store(path) as store:
        assert store.tldr(1) == "An heirloom."


# ── The file ──────────────────────────────────────────────────────────────────


def test_the_list_survives_reopening(tmp_path):
    path = tmp_path / "nested" / "really.db"
    with Store(path) as store:
        store.add("https://example.com/bitter", LESSON, fetched=True)
    with Store(path) as store:
        assert [item.title for item in store.items()] == ["The Bitter Lesson"]
        assert len(store.search("computation")) == 1


def test_a_list_from_before_reads_were_timed_is_upgraded(tmp_path):
    path = tmp_path / "really.db"
    old = sqlite3.connect(path)
    old.executescript(MIGRATIONS[0])
    old.execute("PRAGMA user_version = 1")
    old.execute(
        "INSERT INTO items (url, title, text, content, words, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "https://example.com/old",
            "From Before",
            "an heirloom",
            "an *heirloom*",
            2,
            "2026-01-01T00:00:00+00:00",
        ),
    )
    old.commit()
    old.close()
    with Store(path) as store:
        [item] = store.items()
        assert (item.title, item.words, item.minutes) == ("From Before", 2, 1)
        assert store.content(item.id) == "an *heirloom*"
        assert len(store.search("heirloom")) == 1
        assert store.pace() == Pace(DEFAULT_WPM, 0, 0)
        store.record(Reading(2300, 600, datetime.now(UTC)))
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    with Store(path) as store:  # and opening it again changes nothing
        assert len(store.items()) == len(store.readings()) == 1


def test_a_newer_file_is_refused(tmp_path):
    path = tmp_path / "really.db"
    with Store(path) as store:
        store.db.execute("PRAGMA user_version = 99")
    with pytest.raises(StoreError, match="newer"):
        Store(path)


def test_default_path_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_DB, str(tmp_path / "elsewhere.db"))
    assert default_path() == tmp_path / "elsewhere.db"
    monkeypatch.delenv(ENV_DB)
    assert default_path().name == "really.db"


def test_the_override_can_be_a_folder(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_DB, str(tmp_path))
    assert default_path() == tmp_path / "really.db"
    monkeypatch.setenv(ENV_DB, f"{tmp_path}/not/made/yet/")
    assert default_path() == tmp_path / "not" / "made" / "yet" / "really.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(ENV_DB, "~/reading/list.db")
    assert default_path() == tmp_path / "reading" / "list.db"
