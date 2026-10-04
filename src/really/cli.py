"""`really`: a reading list for your terminal that remembers what you read."""

from __future__ import annotations

import json
import os
import random
import sqlite3
import stat
import sys
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn, TextIO

import httpx
import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from . import __version__, render, safari, urls
from .clipboard import ClipboardError, read_clipboard
from .expiry import parse_span, site_of
from .export import to_dict, write_markdown
from .extract import Article, extract, same_article
from .fetch import FetchError, Page, fetch, new_client
from .store import (
    ENV_DB,
    Ambiguous,
    Item,
    NotFound,
    State,
    Store,
    StoreError,
    default_path,
    is_lesser,
)
from .summarize import SummaryError
from .summarize import tldr as summarize

MIN_PASTED_WORDS = 20
WORKERS = 6

out = Console(highlight=False)
err = Console(stderr=True, highlight=False)

SETTINGS = {"help_option_names": ["-h", "--help"]}
app = typer.Typer(add_completion=False, rich_markup_mode="rich", context_settings=SETTINGS)
bring = typer.Typer(rich_markup_mode="rich", context_settings=SETTINGS, no_args_is_help=True)
app.add_typer(
    bring,
    name="import",
    help="Bring links in from somewhere else.",
    rich_help_panel="Upkeep",
)

COLLECT, READ, KEEP, UPKEEP = "Collect", "Read", "Keep", "Upkeep"

Ref = Annotated[
    str,
    typer.Argument(metavar="ITEM", help="An id, a URL, or a few words from the title."),
]
OptionalRef = Annotated[
    str | None,
    typer.Argument(
        metavar="[ITEM]",
        help="An id, a URL, or a few words from the title. Defaults to what you last opened.",
        show_default=False,
    ),
]
Tags = Annotated[
    list[str] | None,
    typer.Option(
        "--tag",
        "-t",
        metavar="TAG",
        help="Tag it. Repeat for more.",
        show_default=False,
    ),
]
Note = Annotated[
    str | None,
    typer.Option("--note", "-n", help="Why it's worth keeping. Searchable.", show_default=False),
]
AsJson = Annotated[bool, typer.Option("--json", "-j", help="Print JSON instead.")]


class InputError(Exception):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


def fail(message: str, *, detail: str | None = None, hint: str | None = None) -> NoReturn:
    render.error(err, message, detail=detail, hint=hint)
    raise typer.Exit(1)


# ── Input ─────────────────────────────────────────────────────────────────────


def _stdin_is_piped(stdin: TextIO) -> bool:
    """True when stdin is a pipe or redirected file, not a terminal or /dev/null."""
    try:
        mode = os.fstat(stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISREG(mode) or stat.S_ISSOCK(mode)


def _read(stdin: TextIO) -> str:
    buffer = getattr(stdin, "buffer", None)
    if buffer is not None:
        return buffer.read().decode("utf-8", errors="replace")
    return stdin.read()


def resolve_links(args: list[str] | None, stdin: TextIO | None = None) -> tuple[list[str], str]:
    """Work out which links to add: arguments, piped stdin, or else the clipboard.

    Returns (links, origin) where origin is a short description for display.
    """
    stdin = stdin or sys.stdin
    if args:
        links: list[str] = []
        for arg in args:
            found = urls.find_urls(_read(stdin) if arg == "-" else arg)
            if not found and arg != "-":
                raise InputError(f"“{arg}” doesn't look like a link.")
            links += found
        return list(dict.fromkeys(links)), "stdin" if args == ["-"] else "argument"
    # Launchers and shortcuts tend to attach an empty pipe, which shouldn't hide the clipboard.
    if _stdin_is_piped(stdin) and (piped := _read(stdin)).strip():
        return urls.find_urls(piped), "stdin"
    try:
        return urls.find_urls(read_clipboard()), "clipboard"
    except ClipboardError as e:
        raise InputError(
            str(e),
            hint="Pass the link as an argument instead: [bold]really add https://…[/]",
        ) from e


NO_LINKS = {
    "clipboard": (
        "There's no link on your clipboard.",
        "Copy one first, or pass it directly: [bold]really add https://…[/]",
    ),
    "stdin": ("No links came through stdin.", None),
}


# ── The pieces commands are made of ───────────────────────────────────────────


@contextmanager
def library(sweep_first: bool = False) -> Iterator[Store]:
    """Open the reading list, turning database trouble into a readable error.

    Each command also deletes whatever has expired (see sweep). Commands that list things do
    it first, so what they show is current. The rest do it last, so that a number you give
    still means what it meant when you last saw the list.
    """
    path = default_path()
    try:
        store = Store(path)
    except (StoreError, sqlite3.Error, OSError) as e:
        fail(f"Couldn't open your reading list at {path}.", detail=str(e))
    try:
        if sweep_first:
            sweep(store)
        yield store
        sweep(store)
    except sqlite3.Error as e:
        fail("Something went wrong with the reading list's database.", detail=str(e))
    finally:
        store.close()


def sweep(store: Store) -> None:
    """Delete what has waited unread past its time, and say so.

    There's nothing running in the background, so this is where expiry happens: inside
    whatever command comes next. It's reported on stderr, to leave --json output alone.
    """
    expired = store.expired()
    if not expired:
        return
    with renumbering(store, err):
        for item in expired:
            store.delete(item.id)
            kept = (item.expires_at or item.queued_at) - item.queued_at
            unread = f"unread after {render.span(int(kept.total_seconds()))}"
            render.receipt(err, "Expired", item, unread, style=render.FAINT, mark="✗")
            render.address(err, item)


def current(store: Store, ref: str, state: State | None = None) -> Item:
    """Find the item you mean, let expiry run, and make sure it's still there.

    In that order: the number is looked up before anything ahead of it can expire and
    shift it. If what you named has itself expired, sweep has just said so.
    """
    item = find(store, ref, state)
    sweep(store)
    still_here = store.get(item.id)
    if still_here is None:
        raise typer.Exit(1)
    return still_here


def find(store: Store, ref: str, state: State | None = None) -> Item:
    try:
        return store.resolve(ref, state)
    except Ambiguous as e:
        render.error(err, str(e), hint="Add a word or two, or use the id:")
        render.candidates(err, e.matches)
        raise typer.Exit(1) from e
    except NotFound as e:
        fail(str(e), hint="[bold]really list --all[/] shows everything you have.")


def finished(store: Store, ref: str | None, state: State | None = None) -> Item:
    """The item you mean: the one named, or else the one you opened most recently."""
    if ref:
        return find(store, ref, state)
    item = store.last_opened()
    if item is None:
        fail(
            "Which one?",
            hint="Name it by id or title. With nothing named, really uses whatever you last "
            "opened with [bold]really next[/] or [bold]really open[/].",
        )
    return item


@dataclass(frozen=True, slots=True)
class Capture:
    item: Item
    new: bool
    problem: str = ""  # why the page couldn't be fetched, if it couldn't


def capture(
    store: Store,
    client: httpx.Client,
    link: str,
    *,
    tags: list[str],
    offline: bool = False,
) -> Capture:
    """Put one link on the list, fetching it first to learn what it is and where it leads."""
    address = urls.clean(link)
    if existing := store.by_url(address):
        return Capture(existing, new=False)
    if offline:
        return Capture(store.add(address, tags=tags), new=True)
    try:
        # With its tracking parameters still on: a newsletter's redirector may need them.
        landed, article = understand(link, fetch(urls.page_of(link), client), client)
    except FetchError as e:
        return Capture(store.add(address, tags=tags), new=True, problem=str(e))
    if existing := store.by_url(landed):
        return Capture(existing, new=False)
    return Capture(store.add(landed, article, fetched=True, tags=tags), new=True)


def understand(link: str, page: Page, client: httpx.Client) -> tuple[str, Article]:
    """Work out what a fetched page is, and the address it should be known by."""
    # Being bounced to a login page must not replace the link with the login page's address.
    if urls.is_sign_in(page.url) and not urls.is_sign_in(link):
        raise FetchError("it sends you to a sign-in page")
    article = extract(page)
    return address_of(page, article, client), article


def address_of(page: Page, article: Article, client: httpx.Client) -> str:
    """The address to know a page by: nothing after the path, unless that's part of where it is.

    Most query strings only say how you came by a link (?ref=…, ?share=…), but some are the
    address itself (watch?v=…, item?id=…). If the page gives its own address, that settles it.
    If not, the link is tried without its query string: the same article means the query was
    decoration, and anything else means it stays.
    """
    if article.canonical:
        return urls.clean(article.canonical)
    address = urls.clean(page.url)
    bare = urls.bare(address)
    if bare == address:
        return address
    try:
        without = extract(fetch(bare, client))
    except FetchError:
        return address
    return bare if same_article(article, without) else address


def recapture(store: Store, item: Item, page: Page, client: httpx.Client) -> Item:
    address, article = understand(item.url, page, client)
    return store.set_article(item.id, article, address)


def report_refetch(item: Item, page: Page | FetchError, store: Store, client: httpx.Client) -> bool:
    """Record one freshly fetched page and say what came of it. False if it couldn't be read."""
    try:
        if isinstance(page, FetchError):
            raise page
        address, article = understand(item.url, page, client)
    except FetchError as e:
        render.error(out, f"#{item.ref} {item.name}", detail=f"Couldn't fetch it: {e}.")
        return False
    kept = bool(item.words) and is_lesser(article, item)
    item = store.set_article(item.id, article, address)
    style = render.ARCHIVE if item.archived else render.ACCENT
    if kept:
        offered = f"the page now offers {article.words:,} words, you have {item.words:,}"
        render.receipt(out, "Kept your copy of", item, offered, style=style, mark="◇")
    else:
        extra = f"{item.words:,} words" if item.words else "no readable text found"
        render.receipt(out, "Fetched", item, extra, style=style)
    return True


def fetch_all(client: httpx.Client, links: list[str]) -> Iterator[Page | FetchError]:
    """Fetch several pages at once, yielding each result (or what went wrong) in order."""

    def attempt(link: str) -> Page | FetchError:
        try:
            return fetch(link, client)
        except FetchError as e:
            return e

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        yield from pool.map(attempt, links)


def read_now(store: Store, item: Item) -> None:
    """Open an item in the browser and remember that you did."""
    typer.launch(item.url)
    if not item.archived:
        item = store.mark_opened(item.id)
    out.print()
    out.print(render.card(item, min(out.width, render.READING_WIDTH)))
    if not item.archived:
        hint = (
            "  When you've read it: [bold]really done[/] keeps it, [bold]really delete[/] doesn't."
        )
        out.print(Text.from_markup(hint, style=render.FAINT))
    out.print()


@contextmanager
def renumbering(store: Store, console: Console | None = None) -> Iterator[None]:
    """Afterwards, say which items the block left with a new number or new letters.

    Whatever leaves the queue or the archive, the rest close up behind it. Something that
    changed lists isn't mentioned here: its receipt says what it was and what it is now.
    """
    before = {item.id: (item.state, item.number) for item in store.items()}
    yield
    for state in State:
        moves = []
        for item in store.items(state):
            was = before.get(item.id)
            if was and was[0] is state and was[1] != item.number:
                moves.append((was[1], item.number))
        render.renumbered(console or out, state, moves)


def keep(store: Store, client: httpx.Client, item: Item) -> Item:
    """Archive an item, first fetching a copy if there isn't one yet."""
    problem = ""
    if not item.words:
        try:
            with err.status(render.progress_message("Saving a copy", item.url)):
                item = recapture(store, item, fetch(item.url, client), client)
        except FetchError as e:
            problem = str(e)
    reading = store.finish(item.id)
    was = item.ref
    item = store.archive(item.id)
    saved = f"{item.words:,} words saved" if item.words else "no copy saved"
    render.receipt(out, "Archived", item, saved, style=render.ARCHIVE, mark="◆", was=was)
    if item.paywalled:
        render.note(
            out,
            "It's paywalled, so that's only the free preview. For the whole thing, copy the "
            f"article's text and run [bold]really paste {item.ref}[/].",
        )
    elif not item.words:
        reason = f" ({problem})" if problem else ""
        render.note(
            out,
            f"Couldn't get the text{reason}. The link is archived; "
            f"[bold]really refresh {item.ref}[/] tries again.",
        )
    if reading:
        render.timed(out, reading, store.pace())
    return item


def discard(store: Store, item: Item) -> None:
    """Delete an item. If you'd just read it, that read is timed first."""
    reading = store.finish(item.id)
    store.delete(item.id)
    render.receipt(out, "Deleted", item, style=render.FAINT, mark="✗")
    render.address(out, item)
    if reading:
        render.timed(out, reading, store.pace())


def show_list(
    state: State | None = State.QUEUED,
    tag: str | None = None,
    sort: Sort | None = None,
    reverse: bool = False,
    as_json: bool = False,
) -> None:
    with library(sweep_first=True) as store:
        items = store.items(state, tag)
    if sort is Sort.LENGTH:
        items.sort(key=lambda item: item.words)
    elif sort is Sort.TITLE:
        items.sort(key=lambda item: item.name.casefold())
    if reverse:
        items.reverse()
    if as_json:
        print(json.dumps([to_dict(item) for item in items], indent=2, ensure_ascii=False))
        return
    if items:
        render.listing(out, items)
    if state is State.ARCHIVED:
        render.archive_summary(out, items)
    else:
        render.queue_summary(out, [item for item in items if not item.archived])


# ── Commands ──────────────────────────────────────────────────────────────────


class Sort(StrEnum):
    ADDED = "added"
    LENGTH = "length"
    TITLE = "title"


class ReadPolicy(StrEnum):
    SKIP = "skip"
    QUEUE = "queue"
    ARCHIVE = "archive"


def _version(value: bool) -> None:
    if value:
        out.print(f"really {__version__}")
        raise typer.Exit()


EPILOG = (
    "[bold]Examples[/]\n\n"
    "  [cyan]really add[/]               queue the link on your clipboard\n"
    "  [cyan]really[/]                   see what's waiting\n"
    "  [cyan]really next 15[/]           open something you can read in 15 minutes\n"
    "  [cyan]really done[/]              keep what you just read, text and all\n"
    "  [cyan]really delete[/]            …or don't\n"
    "  [cyan]really search neuralese[/]  find it again, months later\n\n"
    f"Your list lives in one SQLite file. Set [bold]${ENV_DB}[/] to move it."
)


@app.callback(invoke_without_command=True, epilog=EPILOG)
def root(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option("--version", "-V", callback=_version, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """A reading list that remembers what you read.

    Queue links straight from your clipboard and read them when you have time.
    Archive the ones worth keeping, and their full text becomes searchable.

    With no command, [bold]really[/] shows your queue.
    """
    if ctx.invoked_subcommand is None:
        show_list()


@app.command(rich_help_panel=COLLECT)
def add(
    links: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[LINKS]...",
            help="Links to add. Use [bold]-[/] for stdin. Defaults to your clipboard.",
            show_default=False,
        ),
    ] = None,
    tag: Tags = None,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Don't fetch the page; just save the link."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Don't ask before adding several links at once."),
    ] = False,
) -> None:
    """Queue the link on your [bold]clipboard[/] (or ones you pass or pipe in).

    Fetches the page to learn its title, author and length,
    and follows newsletter redirects to the article's real address.

    [bold]really https://…[/] is shorthand for [bold]really add https://…[/].
    """
    try:
        found, origin = resolve_links(links)
    except InputError as e:
        fail(str(e), hint=e.hint)
    if not found:
        message, hint = NO_LINKS.get(origin, ("That doesn't contain a link.", None))
        fail(message, hint=hint)

    if origin != "argument":
        err.print(render.source_line(origin, found, err.width))
    if origin == "clipboard" and len(found) > 1 and not yes and sys.stdin.isatty():
        for link in found:
            err.print(Text(f"  {link}", style=render.FAINT), no_wrap=True, overflow="ellipsis")
        if not typer.confirm(f"Add all {len(found)}?", default=True, err=True):
            raise typer.Exit(0)

    with library(sweep_first=True) as store, new_client() as client:
        for link in found:
            with err.status(render.progress_message("Fetching", link), spinner_style=render.ACCENT):
                result = capture(store, client, link, tags=tag or [], offline=offline)
            item = result.item
            if not result.new:
                render.duplicate(out, item)
                continue
            render.added(out, item, store.count(State.QUEUED))
            if result.problem:
                render.note(
                    out,
                    f"Couldn't fetch it: {result.problem}. The link is saved; "
                    f"[bold]really refresh {item.ref}[/] tries again.",
                )
            elif item.paywalled:
                render.note(out, "It's paywalled: only the free preview could be read.")
            if item.expires_at:
                kept = render.span(int((item.expires_at - item.queued_at).total_seconds()))
                render.note(out, f"Expires in {kept}, unless you've read it by then.")


@app.command("list", rich_help_panel=READ)
def list_(
    archive: Annotated[
        bool,
        typer.Option("--archive", "-a", help="Show the archive instead of the queue."),
    ] = False,
    everything: Annotated[bool, typer.Option("--all", help="Show both.")] = False,
    tag: Annotated[
        str | None,
        typer.Option("--tag", "-t", metavar="TAG", help="Only this tag.", show_default=False),
    ] = None,
    sort: Annotated[
        Sort,
        typer.Option("--sort", "-s", help="Order by when added, reading time, or title."),
    ] = Sort.ADDED,
    reverse: Annotated[bool, typer.Option("--reverse", "-r", help="Flip the order.")] = False,
    as_json: AsJson = False,
) -> None:
    """Show your queue, oldest first."""
    state = None if everything else State.ARCHIVED if archive else State.QUEUED
    show_list(state, tag, sort, reverse, as_json)


app.command("ls", hidden=True)(list_)


@app.command("next", rich_help_panel=READ)
def next_(
    minutes: Annotated[
        int | None,
        typer.Argument(
            min=1,
            help="Only something you can read in this many minutes.",
            show_default=False,
        ),
    ] = None,
    surprise: Annotated[
        bool, typer.Option("--random", "-r", help="Pick at random, not the oldest.")
    ] = False,
    tag: Annotated[
        str | None,
        typer.Option("--tag", "-t", metavar="TAG", help="Only this tag.", show_default=False),
    ] = None,
    peek: Annotated[
        bool, typer.Option("--peek", "-p", help="Say what's next without opening it.")
    ] = False,
) -> None:
    """Open the next thing to read: the one that's been waiting longest."""
    with library(sweep_first=True) as store:
        queue = store.items(State.QUEUED, tag)
        if not queue:
            render.queue_summary(out, [])
            return
        choices = [item for item in queue if 0 < item.minutes <= minutes] if minutes else queue
        if not choices:
            sized = [item.minutes for item in queue if item.minutes]
            shortest = f"The shortest takes {render.duration(min(sized))}." if sized else None
            fail(
                f"Nothing in your queue fits in {render.duration(minutes or 0)}.",
                hint=shortest,
            )
        item = random.choice(choices) if surprise else choices[0]
        if peek:
            out.print()
            out.print(render.card(item, min(out.width, render.READING_WIDTH)))
            out.print()
        else:
            read_now(store, item)


@app.command("open", rich_help_panel=READ)
def open_(ref: Ref) -> None:
    """Open something in your browser."""
    with library() as store:
        read_now(store, current(store, ref))


@app.command(rich_help_panel=READ)
def info(ref: Ref, as_json: AsJson = False) -> None:
    """Show the details of one item: what it is, and what's become of it.

    For the saved copy itself, there's [bold]really show[/].
    """
    with library() as store:
        item = current(store, ref)
    if as_json:
        print(json.dumps(to_dict(item), indent=2, ensure_ascii=False))
    else:
        render.details(out, item)


@app.command(rich_help_panel=READ)
def tldr(ref: Ref) -> None:
    """Have Claude summarize something from its saved text.

    It needs the full text to be saved, and Claude Code's [bold]claude[/] command.
    The summary is kept, so it's only ever asked for once.
    """
    with library() as store:
        item = current(store, ref)
        if not item.words:
            fail(
                f"There's no saved copy of #{item.ref} to summarize.",
                hint=f"[bold]really refresh {item.ref}[/] fetches one.",
            )
        if item.paywalled:
            fail(
                f"Only the free preview of #{item.ref} is saved, not the full text.",
                hint="If you can read the whole thing, copy its text and run "
                f"[bold]really paste {item.ref}[/].",
            )
        summary = store.tldr(item.id)
        if not summary:
            asking = render.progress_message("Asking Claude", item.name)
            try:
                with err.status(asking, spinner_style=render.ACCENT):
                    summary = summarize(
                        title=item.name,
                        author=item.author,
                        site=item.site or item.host,
                        text=store.text(item.id),
                    )
            except SummaryError as e:
                fail(str(e), detail=e.detail, hint=e.hint)
            store.set_tldr(item.id, summary)
    if out.is_terminal:
        render.summary(out, item, summary)
    else:
        print(summary)


@app.command(rich_help_panel=READ)
def review(
    tag: Annotated[
        str | None,
        typer.Option("--tag", "-t", metavar="TAG", help="Only this tag.", show_default=False),
    ] = None,
) -> None:
    """Go through your queue one by one, deciding what to do with each.

    For a queue that's got away from you: open, archive, delete or skip each item in turn.
    """
    tally: Counter[str] = Counter()
    with library(sweep_first=True) as store, new_client() as client:
        queue = store.items(State.QUEUED, tag)
        if not queue:
            render.queue_summary(out, [])
            return
        for position, waiting in enumerate(queue, 1):
            item = store.get(waiting.id) or waiting  # its number may have changed since
            out.print()
            out.print(Text(f"{position} of {len(queue)}", style=render.FAINT))
            out.print(render.card(item, min(out.width, render.READING_WIDTH)))
            choice = "o"
            while choice == "o":
                try:
                    choice = Prompt.ask(
                        "[bold](o)[/]pen  [bold](a)[/]rchive  [bold](d)[/]elete  "
                        "[bold](s)[/]kip  [bold](q)[/]uit",
                        choices=["o", "a", "d", "s", "q"],
                        default="s",
                        show_choices=False,
                        console=out,
                    )
                except (EOFError, KeyboardInterrupt):
                    choice = "q"
                if choice == "o":
                    typer.launch(item.url)
                    store.mark_opened(item.id)
            if choice == "q":
                break
            with renumbering(store):
                if choice == "a":
                    keep(store, client, item)
                    tally["archived"] += 1
                elif choice == "d":
                    discard(store, item)
                    tally["deleted"] += 1
        left = store.count(State.QUEUED)
    done = [f"{count} {what}" for what, count in tally.items()] or ["Nothing changed"]
    out.print()
    summary = f"{', '.join(done).capitalize()} · {left} left in your queue"
    out.print(Text.assemble(("◇ ", render.ACCENT), (summary, "bold")))
    out.print()


@app.command(rich_help_panel=READ)
def pace() -> None:
    """How fast you read, worked out from how long things took you.

    A read is timed from [bold]really next[/] or [bold]really open[/]
    until you archive or delete it. Reading times follow the result.
    """
    with library() as store:
        current, readings = store.pace(), store.readings()
    render.pace(out, current, readings)


def shelve(
    store: Store, client: httpx.Client, item: Item, note: str | None, tags: list[str] | None
) -> None:
    """Archive an item, with a note and tags if given. If it's archived already, update those."""
    if note is not None:
        item = store.edit(item.id, note=note)
    if tags:
        item = store.set_tags(item.id, [*item.tags, *tags])
    if item.archived:
        verb = "Updated" if note is not None or tags else "Already archived"
        render.receipt(out, verb, item, style=render.ARCHIVE, mark="◆")
    else:
        with renumbering(store):
            keep(store, client, item)


@app.command(rich_help_panel=KEEP)
def archive(ref: OptionalRef = None, note: Note = None, tag: Tags = None) -> None:
    """Keep something you've read, with its full text searchable."""
    with library() as store, new_client() as client:
        item = finished(store, ref, State.QUEUED if ref else None)
        shelve(store, client, item, note, tag)


@app.command(rich_help_panel=KEEP)
def done(note: Note = None, tag: Tags = None) -> None:
    """Archive what you last opened: [bold]really archive[/] with nothing named."""
    with library() as store, new_client() as client:
        item = store.last_opened()
        if item is None:
            fail(
                "There's nothing you've opened and not yet finished.",
                hint="[bold]really next[/] opens something to read. To archive something you "
                "didn't open here, name it: [bold]really archive 3[/].",
            )
        shelve(store, client, item, note, tag)


@app.command(rich_help_panel=KEEP)
def delete(
    refs: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[ITEMS]...",
            help="Ids, URLs, or a few words from a title. Defaults to what you last opened.",
            show_default=False,
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Don't ask before deleting from the archive."),
    ] = False,
) -> None:
    """Forget something, whether you read it or just changed your mind."""
    with library() as store:
        # All looked up before any is deleted, since each deletion renumbers the rest. They're
        # reported by what you called them, and what the others are called now is said at the end.
        named = [find(store, ref) for ref in refs] if refs else [finished(store, None)]
        with renumbering(store):
            for item in {item.id: item for item in named}.values():
                if item.archived and not yes:
                    title = Text.assemble((f"#{item.ref} ", render.ARCHIVE), (item.name, "bold"))
                    err.print(title)
                    if not typer.confirm("That's in your archive. Delete it for good?", err=True):
                        continue
                discard(store, item)


app.command("rm", hidden=True)(delete)


@app.command(rich_help_panel=KEEP)
def search(
    query: Annotated[
        list[str], typer.Argument(metavar="WORDS...", help="What to look for.", show_default=False)
    ],
    archive: Annotated[
        bool, typer.Option("--archive", "-a", help="Only look in the archive.")
    ] = False,
    queue: Annotated[bool, typer.Option("--queue", "-q", help="Only look in the queue.")] = False,
    tag: Annotated[
        str | None,
        typer.Option("--tag", "-t", metavar="TAG", help="Only this tag.", show_default=False),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="How many to show.")] = 10,
    as_json: AsJson = False,
) -> None:
    """Search the full text of everything you've saved.

    Every word has to appear; the best matches come first.

    [bold]"exact phrase"[/] finds those words together.
    [bold]comput*[/] finds anything starting that way.
    [bold]author:sutton[/] looks in one place (also title:, site:, tag:, note:).
    """
    text = " ".join(query)
    state = None if archive == queue else State.ARCHIVED if archive else State.QUEUED
    with library(sweep_first=True) as store:
        results = store.search(text, state, tag, limit)
    if as_json:
        print(json.dumps([to_dict(hit.item) for hit in results], indent=2, ensure_ascii=False))
    else:
        render.hits(out, results, text)


@app.command(rich_help_panel=KEEP)
def show(
    ref: Ref,
    no_pager: Annotated[bool, typer.Option("--no-pager", help="Print it all at once.")] = False,
    as_json: AsJson = False,
) -> None:
    """Read the saved copy of something, right here in the terminal.

    Piped somewhere else, it prints the copy as plain Markdown.
    """
    with library() as store:
        item = current(store, ref)
        content = store.content(item.id)
    if as_json:
        print(json.dumps(to_dict(item, content), indent=2, ensure_ascii=False))
    elif not out.is_terminal:
        print(content)
    elif no_pager or item.words < 200:
        render.article(out, item, content)
    else:
        with out.pager(styles=True):
            render.article(out, item, content)


@app.command(rich_help_panel=KEEP)
def requeue(ref: Ref) -> None:
    """Move something from the archive back to your queue."""
    with library() as store:
        item = find(store, ref, State.ARCHIVED)
        if not item.archived:
            fail(f"#{item.ref} is already in your queue.")
        with renumbering(store):
            render.receipt(out, "Requeued", store.requeue(item.id), was=item.ref)


@app.command(rich_help_panel=UPKEEP)
def tag(
    ref: Ref,
    tags: Annotated[
        list[str] | None, typer.Argument(help="Tags to add.", show_default=False)
    ] = None,
    remove: Annotated[
        list[str] | None,
        typer.Option(
            "--remove",
            "-r",
            metavar="TAG",
            help="A tag to take off.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Add or remove tags."""
    with library() as store:
        item = current(store, ref)
        dropped = {t.lower().lstrip("#") for t in remove or []}
        kept = [t for t in item.tags if t not in dropped]
        item = store.set_tags(item.id, [*kept, *(tags or [])])
        style = render.ARCHIVE if item.archived else render.ACCENT
        render.receipt(out, "Tagged" if item.tags else "Untagged", item, style=style)


@app.command(rich_help_panel=UPKEEP)
def edit(
    ref: Ref,
    title: Annotated[str | None, typer.Option("--title", show_default=False)] = None,
    author: Annotated[str | None, typer.Option("--author", show_default=False)] = None,
    site: Annotated[str | None, typer.Option("--site", show_default=False)] = None,
    published: Annotated[
        str | None, typer.Option("--published", metavar="DATE", show_default=False)
    ] = None,
    note: Annotated[str | None, typer.Option("--note", "-n", show_default=False)] = None,
) -> None:
    """Correct a title, author or site, or change your note."""
    given = {
        "title": title,
        "author": author,
        "site": site,
        "published": published,
        "note": note,
    }
    changes = {field: value.strip() for field, value in given.items() if value is not None}
    if not changes:
        fail("Nothing to change.", hint='Say what, like [bold]--author "Rich Sutton"[/].')
    with library() as store:
        item = store.edit(current(store, ref).id, **changes)
        out.print()
        out.print(render.card(item, min(out.width, render.READING_WIDTH)))
        out.print()


@app.command(rich_help_panel=UPKEEP)
def paste(ref: OptionalRef = None) -> None:
    """Save the text on your clipboard as something's copy.

    For pages your browser can read but really can't, like paywalled posts
    you subscribe to. Select the article's text, copy it, and run this.
    """
    try:
        text = read_clipboard().strip()
    except ClipboardError as e:
        fail(str(e))
    words = len(text.split())
    if words < MIN_PASTED_WORDS:
        fail(
            f"Your clipboard only has {render.plural(words, 'word')} on it.",
            hint="Select the article's text in your browser and copy it first.",
        )
    with library() as store:
        target = finished(store, ref)
        sweep(store)
        if store.get(target.id) is None:
            raise typer.Exit(1)  # it has just expired, as sweep said
        item = store.set_text(target.id, text)
        style = render.ARCHIVE if item.archived else render.ACCENT
        render.receipt(out, "Saved your copy of", item, f"{item.words:,} words", style=style)


@app.command(rich_help_panel=UPKEEP)
def refresh(
    refs: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[ITEMS]...",
            help="What to fetch again. Defaults to everything that has no saved copy.",
            show_default=False,
        ),
    ] = None,
    everything: Annotated[
        bool, typer.Option("--all", help="Fetch everything again, queue and archive.")
    ] = False,
) -> None:
    """Fetch pages again, to fill in whatever's missing.

    A copy you already have is only replaced by one that's as good:
    never by a paywalled preview, or by a page that has since lost its text.
    """
    if everything and refs:
        fail("Name some items or pass --all, not both.")
    with library() as store, new_client() as client:
        if refs:
            named = [find(store, ref) for ref in refs]
            sweep(store)
            items = [item for item in map(store.get, (item.id for item in named)) if item]
        else:
            items = [item for item in store.items() if everything or not item.words]
        if not items:
            out.print(Text.assemble(("◇ ", render.ACCENT), "Nothing needs fetching."))
            return
        failures = 0
        with err.status("", spinner_style=render.ACCENT) as status:
            pages = fetch_all(client, [item.url for item in items])
            for done, (item, page) in enumerate(zip(items, pages, strict=True), 1):
                status.update(render.progress_message(f"Fetching {done} of {len(items)}", item.url))
                failures += not report_refetch(item, page, store, client)
    if failures:
        raise typer.Exit(1)


@bring.command("safari")
def import_safari(
    file: Annotated[
        Path,
        typer.Option("--file", "-f", help="Safari's bookmarks file.", show_default=False),
    ] = safari.BOOKMARKS,
    read: Annotated[
        ReadPolicy,
        typer.Option("--read", help="What to do with items Safari says you've already read."),
    ] = ReadPolicy.SKIP,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", "-n", help="Show what would be imported.")
    ] = False,
) -> None:
    """Bring in Safari's Reading List, with its titles and dates.

    Nothing is fetched, and Safari's own list is left as it is.
    Afterwards, [bold]really refresh[/] fills in authors, reading times and text.
    """
    try:
        entries = safari.reading_list(file)
    except safari.SafariError as e:
        fail(str(e), hint=e.hint)
    tally: Counter[str] = Counter()
    with library() as store:
        for entry in entries:
            address = urls.clean(entry.url)
            if entry.read and read is ReadPolicy.SKIP:
                tally["skipped"] += 1
            elif store.by_url(address):
                tally["already here"] += 1
            else:
                archived = entry.read and read is ReadPolicy.ARCHIVE
                tally["archived" if archived else "queued"] += 1
                if dry_run:
                    line = Text.assemble(("  + ", render.ACCENT), entry.title or address)
                    out.print(line, no_wrap=True, overflow="ellipsis")
                    continue
                store.add(
                    address,
                    Article(title=entry.title, summary=entry.preview),
                    state=State.ARCHIVED if archived else State.QUEUED,
                    added_at=entry.added,
                )
    imported = tally["queued"] + tally["archived"]
    verb = "Would import" if dry_run else "Imported"
    out.print(
        Text.assemble(
            ("✓ ", f"bold {render.ACCENT}"),
            (f"{verb} {render.plural(imported, 'link')}", "bold"),
            (" from Safari's Reading List", ""),
        )
    )
    details = [f"{count} {what}" for what, count in tally.items() if count]
    if details:
        out.print(Text("  " + " · ".join(details), style=render.FAINT))
    if tally["skipped"]:
        hint = "  [bold]--read queue[/] or [bold]--read archive[/] brings in the ones you'd read."
        out.print(Text.from_markup(hint, style=render.FAINT))
    if imported and not dry_run:
        hint = "  [bold]really refresh[/] fetches their authors, reading times and text."
        out.print(Text.from_markup(hint, style=render.FAINT))


@app.command("export", rich_help_panel=UPKEEP)
def export_(
    directory: Annotated[
        Path | None,
        typer.Argument(help="Where to write one Markdown file per article.", show_default=False),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", "-j", help="Print everything, queue included, as JSON."),
    ] = False,
) -> None:
    """Export your archive as Markdown files, or everything as JSON."""
    if as_json == (directory is not None):
        fail(
            "Say where to export to, or ask for JSON.",
            hint="[bold]really export ~/notes/reading[/] or [bold]really export --json[/]",
        )
    with library() as store:
        state = None if as_json else State.ARCHIVED
        entries = [(item, store.content(item.id)) for item in store.items(state)]
    if directory is None:
        data = [to_dict(item, content) for item, content in entries]
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    written = write_markdown(directory.expanduser(), entries)
    out.print(
        Text.assemble(
            ("◆ ", f"bold {render.ARCHIVE}"),
            (f"Wrote {render.plural(len(written), 'article')}", "bold"),
            f" to {directory}",
        )
    )


@app.command(rich_help_panel=UPKEEP)
def stats() -> None:
    """How much is waiting, how much you've kept, where it all lives."""
    with library(sweep_first=True) as store:
        numbers = store.stats()
        path = str(store.path).replace(str(Path.home()), "~", 1)
    render.stats(out, numbers, path)


@app.command(rich_help_panel=UPKEEP)
def expire(
    site: Annotated[
        str | None,
        typer.Argument(
            metavar="[SITE]",
            help="A site's address, or a link to something on it.",
            show_default=False,
        ),
    ] = None,
    after: Annotated[
        str | None,
        typer.Argument(
            metavar="[AFTER]",
            help="How long its unread things are kept: 3d, 12h, 2w.",
            show_default=False,
        ),
    ] = None,
    remove: Annotated[
        bool, typer.Option("--remove", "-r", help="Stop expiring things from this site.")
    ] = False,
) -> None:
    """Have unread things from a site deleted once they've waited too long.

    For sources that go stale, like news. [bold]really expire thezvi.substack.com 3d[/]
    deletes anything from there that's still in your queue three days after it joined.
    Only archiving something keeps it; the archive never expires.

    With no site, lists the sites you've set this for.
    """
    with library(sweep_first=site is None) as store:
        if site is None:
            render.expiries(out, store.expiries(), store.items(State.QUEUED))
            return
        domain = site_of(site)
        if not domain:
            fail(
                f"“{site}” doesn't look like a site.",
                hint="Give its address, like [bold]thezvi.substack.com[/], or a link to a post.",
            )
        if remove:
            if not store.remove_expiry(domain):
                fail(f"Nothing from {domain} was set to expire.")
            told = f"Things from {domain} no longer expire."
            out.print(Text.assemble(("✓ ", f"bold {render.ACCENT}"), (told, "bold")))
            return
        if after is None:
            fail(
                "Say how long unread things from there should be kept.",
                hint=f"Like [bold]really expire {domain} 3d[/]. Hours (h), days (d) or weeks (w).",
            )
        try:
            seconds = parse_span(after)
        except ValueError:
            fail(
                f"“{after}” isn't a length of time I can read.",
                hint="Use hours, days or weeks: [bold]12h[/], [bold]3d[/], [bold]2w[/].",
            )
        store.set_expiry(domain, seconds)
        render.expiry_set(out, domain, store.expiries(), store.items(State.QUEUED))


def main() -> None:
    # `really https://…` is short for `really add https://…`.
    if len(sys.argv) > 1 and urls.find_urls(sys.argv[1]):
        sys.argv.insert(1, "add")
    app()
