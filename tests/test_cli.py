import io
import json
import plistlib
from datetime import datetime

import pytest
from typer.testing import CliRunner

from really import cli
from really.clipboard import ClipboardError
from really.store import ENV_DB, State, Store

from .conftest import BLOG, COMMENTS, EMAILED, LONGREAD, PAYWALLED, POST

runner = CliRunner()


class FakeStdin(io.StringIO):
    """A stdin stand-in that looks like a pipe (or not)."""

    def __init__(self, text: str, piped: bool):
        super().__init__(text)
        self.piped = piped


@pytest.fixture(autouse=True)
def fake_pipe_detection(monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_piped", lambda stdin: getattr(stdin, "piped", False))


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path, web):
    """Every test gets its own reading list and the pretend internet."""
    path = tmp_path / "really.db"
    monkeypatch.setenv(ENV_DB, str(path))
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(cli, "new_client", web.client)
    return path


@pytest.fixture
def clipboard(monkeypatch):
    contents = {"text": POST}
    monkeypatch.setattr(cli, "read_clipboard", lambda: contents["text"])
    return contents


@pytest.fixture
def browser(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(cli.typer, "launch", opened.append)
    return opened


def run(*args: str, input: str | None = None):
    return runner.invoke(cli.app, list(args), input=input)


def ok(*args: str, input: str | None = None) -> str:
    result = run(*args, input=input)
    assert result.exit_code == 0, result.output
    return result.output


def items(home, state: State | None = None):
    with Store(home) as store:
        return store.items(state)


# ── Working out which links to add ────────────────────────────────────────────


def test_arguments_win(clipboard):
    assert cli.resolve_links([BLOG, PAYWALLED]) == ([BLOG, PAYWALLED], "argument")


def test_links_are_dug_out_of_arguments(clipboard):
    assert cli.resolve_links([f"<{BLOG}>,", BLOG]) == ([BLOG], "argument")


def test_dash_reads_stdin(clipboard):
    stdin = FakeStdin(f"see {BLOG} and {PAYWALLED}.", piped=False)
    assert cli.resolve_links(["-"], stdin) == ([BLOG, PAYWALLED], "stdin")


def test_piped_stdin_beats_clipboard(clipboard):
    assert cli.resolve_links(None, FakeStdin(BLOG, piped=True)) == ([BLOG], "stdin")


def test_an_empty_pipe_falls_back_to_the_clipboard(clipboard):
    assert cli.resolve_links(None, FakeStdin(" \n", piped=True)) == ([POST], "clipboard")
    assert cli.resolve_links(None, FakeStdin("no links here", piped=True)) == ([], "stdin")


def test_clipboard_is_the_default(clipboard):
    assert cli.resolve_links(None, FakeStdin("", piped=False)) == ([POST], "clipboard")


def test_an_argument_that_is_not_a_link():
    with pytest.raises(cli.InputError, match="doesn't look like a link"):
        cli.resolve_links(["tomorrow"])


def test_missing_clipboard_tool_explains_alternatives(monkeypatch):
    def broken():
        raise ClipboardError("No clipboard tool found")

    monkeypatch.setattr(cli, "read_clipboard", broken)
    with pytest.raises(cli.InputError) as e:
        cli.resolve_links(None, FakeStdin("", piped=False))
    assert "argument" in (e.value.hint or "")


# ── add ───────────────────────────────────────────────────────────────────────


def test_add_from_the_clipboard(home, clipboard):
    output = ok("add")
    assert "clipboard" in output
    assert "Added #1 The Slow Web Is Still Here" in output
    assert "Ada Quill · Margin Notes · 1 min · 1 in your queue" in output
    [item] = items(home)
    assert (item.url, item.author, item.state) == (POST, "Ada Quill", State.QUEUED)


def test_add_follows_an_emailed_link_to_the_post(home, web):
    ok("add", EMAILED)
    [item] = items(home)
    assert item.url == POST  # no token, no tracking, no redirector
    assert item.title == "The Slow Web Is Still Here"


def test_add_from_a_comment_button_link_saves_the_post(home, web):
    button = "https://substack.example/app-link/post?publication_id=1&post_id=2&comments=true"
    web.redirect(button, COMMENTS)
    ok("add", button)
    [item] = items(home)
    assert item.url == POST
    assert item.title == "The Slow Web Is Still Here"
    assert item.words > 200


def test_the_same_post_by_another_route_is_a_duplicate(home, clipboard):
    ok("add")
    output = ok("add", EMAILED)
    assert "Already have #1 The Slow Web Is Still Here" in output
    assert len(items(home)) == 1


def test_a_duplicate_is_spotted_before_fetching(home, web):
    ok("add", BLOG)
    before = len(web.requests)
    assert "Already have" in ok("add", f"{BLOG}?utm_source=newsletter#comments")
    assert len(web.requests) == before


def test_add_several_from_stdin_with_tags(home):
    output = ok("add", "-t", "Later", "-", input=f"{BLOG}\nand {PAYWALLED}\n")
    assert "2 links" in output
    assert [item.tags for item in items(home)] == [("later",), ("later",)]


def test_add_notes_a_paywall(home):
    output = ok("add", PAYWALLED)
    assert "paywalled" in output
    assert "1+ min" in output
    assert items(home)[0].paywalled


def test_add_keeps_a_link_it_cannot_fetch(home, web):
    web.status("https://down.example/essay-on-queues?utm_source=x", 503)
    output = ok("add", "https://down.example/essay-on-queues?utm_source=x")
    assert "Added #1 Essay on queues" in output
    assert "Couldn't fetch it: the site answered 503" in output
    assert "really refresh 1" in output
    [item] = items(home)
    assert item.url == "https://down.example/essay-on-queues"
    assert item.fetched_at is None


def test_add_does_not_mistake_a_sign_in_wall_for_the_article(home, web):
    link = "https://social.example/posts/a-thread-worth-keeping"
    wall = "https://social.example/login?next=%2Fposts%2Fa-thread-worth-keeping"
    web.redirect(link, wall)
    web.serve(wall, "<p>Log in</p>")
    output = ok("add", link)
    assert "sends you to a sign-in page" in output
    [item] = items(home)
    assert item.url == link
    assert item.name == "A thread worth keeping"


def test_add_offline_fetches_nothing(home, web):
    ok("add", "--offline", BLOG)
    assert web.requests == []
    assert items(home)[0].title == ""


def test_add_with_nothing_to_add(clipboard):
    clipboard["text"] = "just some words"
    result = run("add")
    assert result.exit_code == 1
    assert "no link on your clipboard" in result.output


def test_several_links_on_the_clipboard_are_all_added_when_nobody_can_be_asked(home, clipboard):
    clipboard["text"] = f"{BLOG} {PAYWALLED}"
    ok("add")
    assert len(items(home)) == 2


def test_a_url_is_shorthand_for_add(home, monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["really", BLOG, "-t", "habits"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert [(item.url, item.tags) for item in items(home)] == [(BLOG, ("habits",))]


# ── Looking at the list ───────────────────────────────────────────────────────


def test_bare_command_shows_the_queue():
    ok("add", BLOG, POST)
    output = ok()
    assert output.index("Tidy Queues") < output.index("The Slow Web")
    assert "2 things to read" in output
    assert "really next" in output


def test_empty_queue():
    assert "Nothing waiting to be read" in ok()
    assert "Nothing archived yet" in ok("list", "--archive")


def test_list_archive_all_and_tag(home):
    ok("add", BLOG, "-t", "habits")
    ok("add", POST)
    ok("archive", "2")
    assert "Tidy Queues" not in ok("ls", "--archive")
    assert "1 thing kept" in ok("list", "-a")
    everything = ok("list", "--all")
    assert "Tidy Queues" in everything and "The Slow Web" in everything
    assert "The Slow Web" not in ok("list", "--all", "--tag", "habits")


def test_list_sorting():
    ok("add", POST, BLOG)
    by_length = ok("list", "--sort", "length")
    assert by_length.index("Tidy Queues") < by_length.index("The Slow Web")
    by_title = ok("list", "--sort", "title", "--reverse")
    assert by_title.index("Tidy Queues") < by_title.index("The Slow Web")


def test_list_json():
    ok("add", BLOG)
    [data] = json.loads(ok("list", "--json"))
    assert data["title"] == "Tidy Queues, Tidy Mind"
    assert data["author"] == "Sam Okafor"
    assert data["state"] == "queued"


# ── Reading ───────────────────────────────────────────────────────────────────


def test_next_opens_the_oldest_and_remembers(home, browser):
    ok("add", BLOG, POST)
    output = ok("next")
    assert browser == [BLOG]
    assert "Tidy Queues, Tidy Mind" in output
    assert "really archive" in output
    assert items(home)[0].opened_at is not None
    assert "▸" in ok()


def test_next_within_a_time_limit(browser):
    ok("add", BLOG)
    result = run("next", "0")
    assert result.exit_code == 2  # not a sensible number of minutes
    ok("next", "5")
    assert browser == [BLOG]


def test_next_when_nothing_fits(home, browser):
    ok("add", BLOG)
    with Store(home) as store:
        store._set(1, words=23_000)
    result = run("next", "10")
    assert result.exit_code == 1
    assert "Nothing in your queue fits in 10 min" in result.output
    assert "The shortest takes 1 h 40 min" in result.output
    assert browser == []


def test_next_peek_and_tag(home, browser):
    ok("add", BLOG)
    ok("add", POST, "-t", "web")
    assert "The Slow Web" in ok("next", "--peek", "--tag", "web")
    assert browser == []
    assert all(item.opened_at is None for item in items(home))


def test_next_with_an_empty_queue(browser):
    assert "Nothing waiting" in ok("next")
    assert browser == []


def test_open_by_words(browser):
    ok("add", BLOG, POST)
    ok("open", "slow web")
    assert browser == [POST]


def test_open_something_unknown(browser):
    result = run("open", "zebra")
    assert result.exit_code == 1
    assert 'Nothing matches "zebra"' in result.output


def test_an_ambiguous_name_lists_the_candidates(browser):
    ok("add", BLOG, POST)
    result = run("open", "example")
    assert result.exit_code == 1
    assert "matches 2 things" in result.output
    assert "#1 Tidy Queues" in result.output and "#2 The Slow Web" in result.output
    assert browser == []


# ── Finishing ─────────────────────────────────────────────────────────────────


def test_archive_what_was_just_read(home, browser):
    ok("add", BLOG, POST)
    ok("next")
    output = ok("archive", "--note", "The weekly pass", "-t", "habits")
    assert "Archived #1 Tidy Queues, Tidy Mind" in output
    assert "words saved" in output
    [kept] = items(home, State.ARCHIVED)
    assert (kept.note, kept.tags) == ("The weekly pass", ("habits",))
    assert [item.id for item in items(home, State.QUEUED)] == [2]


def test_archive_with_nothing_opened_asks_which():
    ok("add", BLOG)
    result = run("archive")
    assert result.exit_code == 1
    assert "Which one?" in result.output


def test_archive_fetches_a_copy_if_there_is_none(home, web):
    ok("add", "--offline", BLOG)
    output = ok("archive", "1")
    assert "Archived #1 Tidy Queues, Tidy Mind" in output
    [kept] = items(home, State.ARCHIVED)
    assert kept.words > 100 and kept.author == "Sam Okafor"


def test_archive_keeps_the_link_even_without_a_copy(home, web):
    web.status("https://down.example/essay", 500)
    ok("add", "https://down.example/essay")
    output = ok("archive", "1")
    assert "no copy saved" in output
    assert "Couldn't get the text (the site answered 500" in output
    assert len(items(home, State.ARCHIVED)) == 1


def test_archive_warns_about_a_paywalled_preview():
    ok("add", PAYWALLED)
    output = ok("archive", "1")
    assert "only the free preview" in output
    assert "really paste 1" in output


def test_archive_again_updates_the_note(home):
    ok("add", BLOG)
    ok("archive", "1")
    assert "Already archived" in ok("archive", "1")
    assert "Updated" in ok("archive", "1", "--note", "Second thoughts")
    assert items(home)[0].note == "Second thoughts"


def test_delete_what_was_just_read(home, browser):
    ok("add", BLOG, POST)
    ok("open", "2")
    output = ok("delete")
    assert "Deleted #2 The Slow Web Is Still Here" in output
    assert POST in output  # so it can be recovered from scrollback
    assert [item.id for item in items(home)] == [1]


def test_delete_several_by_id(home):
    ok("add", BLOG, POST, PAYWALLED)
    ok("rm", "1", "3")
    assert [item.id for item in items(home)] == [2]


def test_deleting_from_the_archive_asks_first(home):
    ok("add", BLOG)
    ok("archive", "1")
    assert "Deleted" not in ok("delete", "1", input="n\n")
    assert len(items(home)) == 1
    assert "Deleted" in ok("delete", "1", input="y\n")
    assert items(home) == []


def test_delete_yes_skips_the_question(home):
    ok("add", BLOG)
    ok("archive", "1")
    ok("delete", "--yes", "1")
    assert items(home) == []


def test_requeue(home):
    ok("add", BLOG)
    ok("archive", "1")
    assert "Back in your queue" in ok("requeue", "tidy")
    assert items(home)[0].state is State.QUEUED
    assert run("requeue", "1").exit_code == 1


# ── Timing ────────────────────────────────────────────────────────────────────


def length(home, id: int = 1) -> int:
    with Store(home) as store:
        words = store.resolve(str(id)).words
    assert 1600 <= words < 2100  # what the timings in these tests assume of the long read
    return words


def test_archiving_what_you_read_times_it(home, browser, clock):
    ok("add", LONGREAD)
    ok("next")
    clock.advance(minutes=8)
    output = ok("archive")
    assert f"Read in 8 min, at {round(length(home) / 8)} words a minute." in output
    assert "That's 1 of the 5 timed reads needed to work out your pace." in output


def test_deleting_what_you_read_times_it_too(home, browser, clock):
    ok("add", LONGREAD)
    words = length(home)
    ok("open", "1")
    clock.advance(minutes=5)
    assert f"Read in 5 min, at {round(words / 5)} words a minute." in ok("delete")
    with Store(home) as store:
        assert len(store.readings()) == 1


@pytest.mark.parametrize("minutes", [0.2, 240])  # archived at a glance; left open over lunch
def test_a_timing_that_could_not_be_reading_is_dropped(home, browser, clock, minutes):
    ok("add", LONGREAD)
    ok("next")
    clock.advance(minutes=minutes)
    assert "Read in" not in ok("archive")
    with Store(home) as store:
        assert store.readings() == []


def test_archiving_something_you_never_opened_is_not_timed(home, clock):
    ok("add", LONGREAD)
    clock.advance(minutes=8)
    assert "Read in" not in ok("archive", "1")


def read_five(clock, then: str) -> str:
    """Read parts 1 to 5: three in five minutes each, one slowly, one fast."""
    last = ""
    for n, minutes in enumerate([5, 5, 20, 5, 3.5], 1):
        ok("open", str(n))
        clock.advance(minutes=minutes)
        last = ok(then, str(n))
    return last


def test_reading_times_come_to_follow_your_pace(home, browser, clock):
    added = ok("add", *[f"{LONGREAD}?part={n}" for n in range(1, 7)])
    words = length(home)
    assert f" {round(words / 230)} min" in added
    last = read_five(clock, then="archive")
    yours = round(words / 5)  # the three steady reads; the slow and fast ones are set aside
    assert f"Your pace is {yours}, going by 5 timed reads." in last
    listing = ok()
    assert "1 thing to read · about 5 min" in listing
    assert f" {round(words / 230)} min" not in listing


def test_pace_command(home, browser, clock):
    assert "Reading times assume 230 words a minute for now" in ok("pace")
    ok("add", *[f"{LONGREAD}?part={n}" for n in range(1, 6)])
    words = length(home)
    read_five(clock, then="delete")
    output = ok("pace")
    assert f"You read about {round(words / 5)} words a minute" in output
    assert "with the fastest and slowest set aside" in output
    assert "slowest, set aside" in output and "fastest, set aside" in output
    assert f"{round(words / 5)} words a minute · going by 5 timed reads" in ok("stats")


def test_review_times_what_you_read_before_deciding(home, browser, clock, monkeypatch):
    ok("add", LONGREAD)
    opening = Store.mark_opened

    def open_and_read(store, id):
        item = opening(store, id)
        clock.advance(minutes=8)
        return item

    monkeypatch.setattr(Store, "mark_opened", open_and_read)
    output = ok("review", input="o\na\n")
    assert f"Read in 8 min, at {round(length(home) / 8)} words a minute." in output


# ── Finding things again ──────────────────────────────────────────────────────


def test_search_the_full_text():
    ok("add", BLOG, POST)
    ok("archive", "2")
    output = ok("search", "lighthouse")
    assert "#2 The Slow Web Is Still Here" in output
    assert "Ada Quill · Margin Notes · archived just now" in output
    assert "the lighthouse problem" in output
    assert "1 match for lighthouse" in output


def test_search_covers_notes_and_can_be_limited_to_the_archive():
    ok("add", BLOG, POST)
    ok("archive", "1", "--note", "Thursday ritual")
    assert "#1 Tidy Queues" in ok("search", "ritual")
    assert "Nothing matches" in ok("search", "--queue", "ritual")
    assert "#1 Tidy Queues" in ok("search", "--archive", "author:okafor", "promise")
    assert "#2" in ok("search", "-q", "reading")


def test_search_json_and_limit():
    ok("add", BLOG, POST)
    hits = json.loads(ok("search", "--json", "reading", "list"))
    assert {hit["id"] for hit in hits} == {1, 2}
    assert len(json.loads(ok("search", "--json", "-n", "1", "reading", "list"))) == 1


def test_search_with_nothing_searchable():
    ok("add", BLOG)
    assert "Nothing matches" in ok("search", "—")


def test_show_prints_the_saved_copy_when_piped():
    ok("add", POST)
    output = ok("show", "1")
    assert output.startswith("Every few months somebody announces")
    assert "[a dozen inboxes](https://example.org/inboxes)" in output


def test_show_json_includes_the_copy():
    ok("add", POST)
    data = json.loads(ok("show", "slow", "--json"))
    assert data["title"] == "The Slow Web Is Still Here"
    assert "lighthouse" in data["content"]


# ── Upkeep ────────────────────────────────────────────────────────────────────


def test_tag_and_untag(home):
    ok("add", BLOG)
    assert "#habits #weekly" in ok("tag", "1", "Habits", "weekly")
    assert "Tagged" in ok("tag", "1", "--remove", "habits")
    assert items(home)[0].tags == ("weekly",)
    assert "Untagged" in ok("tag", "1", "-r", "#weekly")


def test_edit(home):
    ok("add", BLOG)
    output = ok("edit", "1", "--author", "S. Okafor", "--note", " Reread yearly ")
    assert "S. Okafor" in output and "Reread yearly" in output
    [item] = items(home)
    assert (item.author, item.note, item.title) == (
        "S. Okafor",
        "Reread yearly",
        "Tidy Queues, Tidy Mind",
    )


def test_edit_needs_something_to_change():
    ok("add", BLOG)
    assert run("edit", "1").exit_code == 1


def test_paste_replaces_a_paywalled_preview(home, clipboard):
    ok("add", PAYWALLED)
    clipboard["text"] = "The whole essay, including the ending about the harbour. " * 8
    output = ok("paste", "1")
    assert "Saved your copy of #1 What the Archive Is For" in output
    [item] = items(home)
    assert not item.paywalled and item.words == 72
    assert "#1" in ok("search", "harbour")


def test_paste_wants_an_article_not_a_link(clipboard):
    ok("add", PAYWALLED)
    clipboard["text"] = PAYWALLED
    result = run("paste", "1")
    assert result.exit_code == 1
    assert "only has 1 word" in result.output


def test_refresh_fills_in_what_is_missing(home):
    ok("add", "--offline", BLOG, POST)
    ok("add", PAYWALLED)
    output = ok("refresh")
    assert "Fetched #1 Tidy Queues, Tidy Mind" in output
    assert "Fetched #2 The Slow Web Is Still Here" in output
    assert "#3" not in output  # already had its text
    assert all(item.words for item in items(home))
    assert "Nothing needs fetching" in ok("refresh")


def test_refresh_all_fetches_everything_again(home, web):
    ok("add", BLOG, POST)
    ok("archive", "2")
    before = len(web.requests)
    output = ok("refresh", "--all")
    assert "Fetched #1" in output and "Fetched #2" in output
    assert len(web.requests) == before + 2
    assert run("refresh", "--all", "1").exit_code == 1  # one or the other


def test_refresh_keeps_a_copy_you_pasted(home, clipboard):
    ok("add", PAYWALLED)
    clipboard["text"] = "The whole essay, including the ending about the harbour. " * 40
    ok("paste", "1")
    output = ok("refresh", "1")
    assert "Kept your copy of #1" in output
    [item] = items(home)
    assert item.words == 360 and not item.paywalled
    assert "#1" in ok("search", "harbour")


def test_refresh_reports_failures(home, web):
    web.status("https://down.example/essay", 500)
    ok("add", "https://down.example/essay", BLOG)
    result = run("refresh", "1", "2")
    assert result.exit_code == 1
    assert "Couldn't fetch it: the site answered 500" in result.output
    assert "Fetched #2" in result.output


def test_review_walks_the_queue(home, browser):
    ok("add", BLOG, POST, PAYWALLED)
    output = ok("review", input="o\na\nd\ns\n")
    assert "1 of 3" in output and "3 of 3" in output
    assert browser == [BLOG]
    assert "1 archived, 1 deleted · 1 left in your queue" in output
    assert [item.id for item in items(home, State.ARCHIVED)] == [1]
    assert [item.id for item in items(home, State.QUEUED)] == [3]


def test_review_can_be_quit(home):
    ok("add", BLOG, POST)
    assert "Nothing changed · 2 left" in ok("review", input="q\n")
    assert "Nothing changed · 2 left" in ok("review", input="")  # stdin closed


@pytest.fixture
def bookmarks(tmp_path):
    def entry(url, title, day, read=False):
        details = {"DateAdded": datetime(2026, 1, day), "PreviewText": f"About {title}."}
        if read:
            details["DateLastViewed"] = datetime(2026, 2, day)
        return {"URLString": url, "URIDictionary": {"title": title}, "ReadingList": details}

    children = [
        entry(f"{BLOG}?utm_source=share", "Tidy Queues", 3),
        entry("https://example.com/unread", "Unread", 2),
        entry("https://example.com/read", "Read", 1, read=True),
    ]
    path = tmp_path / "Bookmarks.plist"
    path.write_bytes(
        plistlib.dumps({"Children": [{"Title": "com.apple.ReadingList", "Children": children}]})
    )
    return str(path)


def test_import_safari(home, bookmarks):
    ok("add", "--offline", BLOG)
    output = ok("import", "safari", "--file", bookmarks)
    assert "Imported 1 link from Safari's Reading List" in output
    assert "1 skipped" in output and "1 already here" in output
    assert "--read archive" in output and "really refresh" in output
    unread = items(home)[0]  # it keeps the date Safari had, so it sorts first
    assert (unread.title, unread.summary) == ("Unread", "About Unread.")
    assert unread.added_at.date().isoformat() == "2026-01-02"


def test_import_safari_read_items(home, bookmarks):
    ok("import", "safari", "-f", bookmarks, "--read", "archive")
    assert [item.title for item in items(home, State.ARCHIVED)] == ["Read"]
    assert [item.title for item in items(home, State.QUEUED)] == ["Unread", "Tidy Queues"]


def test_import_safari_dry_run(home, bookmarks):
    output = ok("import", "safari", "-f", bookmarks, "--read", "queue", "--dry-run")
    assert "Would import 3 links" in output
    assert "+ Unread" in output
    assert items(home) == []


def test_import_safari_without_a_file(tmp_path):
    result = run("import", "safari", "--file", str(tmp_path / "missing.plist"))
    assert result.exit_code == 1
    assert "no Safari bookmarks file" in result.output


def test_export_markdown(tmp_path):
    ok("add", BLOG, POST)
    ok("archive", "2")
    target = tmp_path / "out"
    assert "Wrote 1 article" in ok("export", str(target))
    [file] = target.iterdir()
    assert file.name == "the-slow-web-is-still-here-2.md"
    assert "lighthouse" in file.read_text()


def test_export_json_is_everything():
    ok("add", BLOG, POST)
    ok("archive", "2")
    data = json.loads(ok("export", "--json"))
    assert [entry["state"] for entry in data] == ["queued", "archived"]
    assert all(entry["content"] for entry in data)


def test_export_needs_a_destination():
    assert run("export").exit_code == 1


def test_stats(home):
    ok("add", BLOG, POST, "-t", "web")
    ok("archive", "2")
    output = ok("stats")
    assert "1 thing to read" in output
    assert "1 thing kept" in output
    assert "#web\u00a02" in output
    assert "really.db" in "".join(output.split())  # a long path folds across lines


def test_version():
    assert ok("--version").startswith("really ")


def test_an_unopenable_list_is_explained(monkeypatch, tmp_path):
    (tmp_path / "blocker").write_text("a file where a directory should be")
    monkeypatch.setenv(ENV_DB, str(tmp_path / "blocker" / "really.db"))
    result = run()
    assert result.exit_code == 1
    assert "Couldn't open your reading list" in result.output
