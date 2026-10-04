"""Regenerate the README screenshots in docs/.

    uv run scripts/screenshots.py

Each one is a made-up reading list, put through the same store, search and rendering code
really uses for a real one, then saved with rich's SVG export. The Substack-shaped posts in
tests/fixtures supply the articles that get searched.
"""

from __future__ import annotations

import io
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from really import render
from really.extract import Article, extract
from really.fetch import Page
from really.pace import Reading
from really.store import Item, State, Store

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
DOCS = ROOT / "docs"
PROMPT = "\u276f "  # a shell-prompt chevron
NOW = datetime.now(UTC)

THEME = TerminalTheme(
    background=(16, 18, 25),
    foreground=(226, 228, 236),
    normal=[
        (32, 34, 44),
        (242, 80, 110),
        (31, 191, 143),
        (235, 154, 18),
        (124, 131, 247),
        (168, 113, 247),
        (86, 182, 194),
        (200, 202, 212),
    ],
    bright=[
        (92, 96, 112),
        (255, 110, 136),
        (70, 214, 170),
        (250, 184, 60),
        (152, 158, 255),
        (190, 146, 255),
        (120, 208, 220),
        (255, 255, 255),
    ],
)

EMAILED = "https://substack.com/app-link/post?publication_id=48213&post_id=1903377&token=eyJ1c2Vy"
POST = "https://www.marginnotes.example/p/the-slow-web-is-still-here"
PAYWALLED = "https://www.marginnotes.example/p/what-the-archive-is-for"


def terminal(width: int) -> Console:
    return Console(
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        file=io.StringIO(),
    )


def command(console: Console, line: str) -> None:
    console.print(Text.assemble((PROMPT, f"bold {render.ACCENT}"), (line, "bold")))


def archive(console: Console, store: Store, item: Item, reading: Reading | None = None) -> None:
    """What `really archive` prints: what the item was and is now called, and who else moved."""
    was = item.ref
    before = {other.id: other.number for other in store.items(State.QUEUED)}
    item = store.archive(item.id)
    extra = f"{item.words:,} words saved"
    render.receipt(console, "Archived", item, extra, style=render.ARCHIVE, mark="◆", was=was)
    if reading:
        render.timed(console, reading, store.pace())
    queue = store.items(State.QUEUED)
    moves = [(before[o.id], o.number) for o in queue if before[o.id] != o.number]
    render.renumbered(console, State.QUEUED, moves)


def fixture(name: str, url: str) -> Article:
    return extract(Page(url, "text/html", (FIXTURES / name).read_bytes(), "utf-8"))


def made_up(title: str, author: str, site: str, minutes: int, summary: str = "") -> Article:
    text = "word " * (minutes * 230)
    return Article(title=title, author=author, site=site, summary=summary, text=text, content=text)


def seed(store: Store) -> None:
    """A queue with some history: stale things, a paywalled preview, something half-read."""

    def add(url: str, article: Article, days: float, *tags: str, state: State = State.QUEUED):
        when = NOW - timedelta(days=days)
        return store.add(url, article, fetched=True, tags=tags, state=state, added_at=when)

    add(
        "https://longinbox.example/redirects",
        made_up("A Field Guide to Newsletter Redirects", "Tom Bell", "The Long Inbox", 7),
        96,
    )
    add(
        "https://priyaraman.example/fts5",
        made_up("Full-Text Search in SQLite, Gently", "Priya Raman", "priyaraman.example", 18),
        41,
        "sqlite",
    )
    kept = add(
        "https://fieldnotes.example/posts/tidy-queues",
        fixture("blog_post.html", "https://fieldnotes.example/posts/tidy-queues"),
        30,
        "habits",
        state=State.ARCHIVED,
    )
    add(PAYWALLED, fixture("substack_paywalled.html", PAYWALLED), 9)
    opened = add(
        "https://papers.example/abs/2609.01234",
        made_up(
            "Attention Spans Are Fine, Actually",
            "J. Morrow, L. Okonkwo, R. Sato",
            "Journal of Reading Research",
            31,
        ),
        2,
        "papers",
    )
    add(
        "https://longinbox.example/unread",
        made_up("Unread Is Not a Moral Failing", "Tom Bell", "The Long Inbox", 4),
        0.2,
    )
    store.edit(kept.id, note="The weekly pass: decide on everything, once a week")
    store.mark_opened(opened.id)


def hero(store: Store) -> Console:
    """Adding the link from an email, then looking at the queue."""
    console = terminal(104)
    command(console, "really add")
    console.print(render.source_line("clipboard", [EMAILED], console.width - 30))
    item = store.add(POST, fixture("substack_post.html", POST), fetched=True)
    render.added(console, item, store.count(State.QUEUED))
    console.print()
    command(console, "really")
    queue = store.items(State.QUEUED)
    render.listing(console, queue, now=NOW)
    render.queue_summary(console, queue, now=NOW)
    return console


def search(store: Store) -> Console:
    """Archiving what you just read, then finding it again by something it said."""
    console = terminal(92)
    item = store.resolve("slow web")
    store.mark_opened(item.id)
    command(console, 'really archive --note "The lighthouse problem" --tag reading')
    store.edit(item.id, note="The lighthouse problem")
    archive(console, store, store.set_tags(item.id, ["reading"]))
    console.print()
    command(console, "really search weekly decide")
    render.hits(console, store.search("weekly decide"), "weekly decide", now=NOW)
    command(console, "really search lighthouse")
    render.hits(console, store.search("lighthouse"), "lighthouse", now=NOW)
    return console


def pace(store: Store) -> Console:
    """Finishing an article, and the reading pace measured from that and the reads before it."""
    console = terminal(92)
    # Words, minutes it took, and how many days ago: mostly steady, one dawdle, one skim.
    history = [(2140, 8.3, 9), (3310, 31, 7), (1460, 5.4, 6), (2890, 10.9, 4), (1980, 3.8, 3)]
    history += [(4120, 16.2, 2), (1730, 6.9, 1)]
    for words, minutes, days in history:
        store.record(Reading(words, round(minutes * 60), NOW - timedelta(days=days)))

    item = store.resolve("attention spans")
    reading = Reading(item.words, 29 * 60, NOW)
    store.record(reading)
    command(console, "really archive")
    archive(console, store, item, reading)
    console.print()
    command(console, "really pace")
    render.pace(console, store.pace(), store.readings(), now=NOW)
    return console


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch, Store(Path(scratch) / "really.db") as store:
        seed(store)
        hero(store).save_svg(str(DOCS / "hero.svg"), title="really", theme=THEME)
        search(store).save_svg(str(DOCS / "search.svg"), title="really search", theme=THEME)
        pace(store).save_svg(str(DOCS / "pace.svg"), title="really pace", theme=THEME)

    for path in sorted(DOCS.glob("*.svg")):
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
