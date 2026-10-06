"""Everything you see: the queue, receipts, search results and the saved copy of an article."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import urls
from .expiry import covering
from .pace import FASTEST_WPM, NEEDED, SHORTEST_WORDS, SLOWEST_WPM, Pace, Reading, middle
from .store import MARK, UNMARK, Hit, Item, State, Stats, letters

MAX_WIDTH = 110
READING_WIDTH = 88

ACCENT = "#4cc2a8"
ARCHIVE = "#9d8cf7"
MATCH = "#f5c451"
STALE = "#eb9a12"
ERROR = "#f2506e"
FAINT = "grey42"

# Beyond this many separate stretches, a renumbering is summed up, not spelt out.
MOST_RUNS = 4

# How long something can sit in the queue before its age starts to stand out.
STALE_DAYS = 30
ANCIENT_DAYS = 90


def _width(console: Console) -> int:
    return min(console.width, MAX_WIDTH)


def plural(n: int, word: str) -> str:
    ending = "" if n == 1 else "es" if word.endswith(("ch", "sh", "s", "x")) else "s"
    return f"{n:,} {word}{ending}"


def _now() -> datetime:
    return datetime.now(UTC)


def ago(moment: datetime, now: datetime | None = None) -> str:
    """A compact age: 5m, 3h, 2d, 6w, 4mo, 2y."""
    seconds = ((now or _now()) - moment).total_seconds()
    minutes, hours, days = seconds / 60, seconds / 3600, seconds / 86400
    if minutes < 1:
        return "now"
    if hours < 1:
        return f"{int(minutes)}m"
    if days < 1:
        return f"{int(hours)}h"
    if days < 14:
        return f"{int(days)}d"
    if days < 70:
        return f"{int(days / 7)}w"
    if days < 365:
        return f"{int(days / 30.44)}mo"
    return f"{int(days / 365.25)}y"


def _when(moment: datetime, now: datetime | None = None) -> str:
    age = ago(moment, now)
    return "just now" if age == "now" else f"{age} ago"


def left(moment: datetime, now: datetime | None = None) -> str:
    """How long until a moment, compactly: 2d left, 5h left, 40m left. Once it's past, "due"."""
    seconds = (moment - (now or _now())).total_seconds()
    if seconds <= 0:
        return "due"
    if seconds < 3600:
        return f"{max(1, int(seconds / 60))}m left"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h left"
    return f"{int(seconds / 86400)}d left"


def span(seconds: int) -> str:
    """A length of time in the largest unit that fits it exactly: 3 days, 36 hours, 2 weeks."""
    for unit, size in (("week", 604800), ("day", 86400)):
        if seconds % size == 0:
            return plural(seconds // size, unit)
    hours = seconds / 3600
    return f"{hours:g} hour{'' if hours == 1 else 's'}"


def duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def took(seconds: int) -> str:
    """How long a read took: 45 s, 6 min, 1 h 5 min."""
    return f"{seconds} s" if seconds < 90 else duration(round(seconds / 60))


def reading_time(item: Item) -> str:
    """How long an item takes to read. A + means at least: we only have the free preview.

    A PDF with no text to count, a scan, is given by its pages instead.
    """
    if not item.words:
        return f"{item.pages} pp" if item.pages else ""
    return f"{item.minutes}{'+' if item.paywalled else ''} min"


def authors(item: Item) -> str:
    names = item.author.split(", ")
    return f"{names[0]} et al." if len(names) > 2 else item.author


def byline(item: Item) -> str:
    """Who wrote it and where: "Scott Alexander · Astral Codex Ten"."""
    author, site = authors(item), item.site or item.host
    if author.casefold() in site.casefold():  # a site named after its author says it all
        return site
    return " · ".join(part for part in (author, site) if part)


def _age_style(item: Item, now: datetime | None) -> str:
    if item.archived:
        return FAINT
    days = ((now or _now()) - item.added_at).days
    if days >= ANCIENT_DAYS:
        return ERROR
    return STALE if days >= STALE_DAYS else FAINT


def _tags(item: Item) -> Text:
    return Text(" ".join(f"#{tag}" for tag in item.tags), style=ARCHIVE)


def _title(item: Item) -> Text:
    text = Text(item.name, style="bold")
    if item.tags:
        text.append("  ").append_text(_tags(item))
    return text


def _marked(text: str, style: str = "") -> Text:
    """Turn a search result's MARK…UNMARK spans into highlights."""
    out = Text(style=style)
    for i, piece in enumerate(text.replace(UNMARK, MARK).split(MARK)):
        out.append(piece, style=f"bold {MATCH}" if i % 2 else "")
    return out


# ── Lists ─────────────────────────────────────────────────────────────────────


def listing(console: Console, items: list[Item], *, now: datetime | None = None) -> None:
    """One row per item, oldest at the top."""
    width = _width(console)
    archived = bool(items) and all(item.archived for item in items)
    table = Table(box=None, pad_edge=False, padding=(0, 1), header_style=FAINT, width=width)
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Title", ratio=1, no_wrap=True, overflow="ellipsis")
    # Titles get the room; who wrote them gets a third of the line at most.
    table.add_column("From", max_width=width // 3, no_wrap=True, overflow="ellipsis", style=FAINT)
    table.add_column("Read", justify="right", no_wrap=True)
    table.add_column("Kept" if archived else "Added", justify="right", no_wrap=True)
    for item in items:
        moment = item.archived_at if archived and item.archived_at else item.added_at
        age = Text(ago(moment, now), style=_age_style(item, now))
        if item.expires_at:  # it's from a site whose unread things get deleted
            age.append(f" · {left(item.expires_at, now)}", style=STALE)
        table.add_row(
            _number(item, mixed=not archived),
            _title(item),
            byline(item),
            reading_time(item) or Text("—", style=FAINT),
            age,
        )
    console.print()
    console.print(table)


def _number(item: Item, mixed: bool) -> Text:
    """What an item is called, with a mark if you've opened it (or, among queued ones, archived)."""
    color = ARCHIVE if item.archived else ACCENT
    mark = ("◆ " if mixed else "") if item.archived else "▸ " if item.opened_at else ""
    return Text.assemble((mark, color), (item.ref, f"bold {color}"))


def queue_summary(console: Console, items: list[Item], *, now: datetime | None = None) -> None:
    """The line under the queue: how much there is to read, and what to do about it."""
    console.print()
    if not items:
        console.print(Text.assemble(("◇ ", ACCENT), ("Nothing waiting to be read.", "bold")))
        console.print(Text.from_markup("  Copy a link, then run [bold]really add[/]."))
        console.print()
        return
    minutes = sum(item.minutes for item in items)
    parts = [plural(len(items), "thing") + " to read"]
    if minutes:
        parts.append(f"about {duration(minutes)}")
    parts.append(f"oldest added {_when(min(item.added_at for item in items), now)}")
    console.print(
        Text.assemble(("◇ ", ACCENT), (parts[0], "bold"), (" · ", FAINT)).append(
            Text(" · ".join(parts[1:]), style=FAINT)
        )
    )
    started = [item for item in items if item.opened_at]
    hint = (
        f"  [{ACCENT}]▸[/] opened: [bold]really done[/] or [bold]really delete[/] "
        "when you've read it"
        if started
        else "  [bold]really next[/] opens the one at the top"
    )
    console.print(Text.from_markup(hint, style=FAINT))
    console.print()


def archive_summary(console: Console, items: list[Item]) -> None:
    console.print()
    if not items:
        console.print(Text.assemble(("◆ ", ARCHIVE), ("Nothing archived yet.", "bold")))
        console.print(Text.from_markup("  [bold]really done[/] keeps what you've just read."))
    else:
        words = sum(item.words for item in items)
        console.print(
            Text.assemble(
                ("◆ ", ARCHIVE),
                (f"{plural(len(items), 'thing')} kept", "bold"),
                (f" · {plural(words, 'word')} to search", FAINT),
            )
        )
    console.print()


# ── Receipts: one or two lines saying what just happened ──────────────────────


def _facts(item: Item, *extra: str) -> Text:
    parts = [byline(item), reading_time(item), *extra]
    return Text("  " + " · ".join(part for part in parts if part), style=FAINT)


def receipt(
    console: Console,
    verb: str,
    item: Item,
    *extra: str,
    style: str = ACCENT,
    mark: str = "✓",
    was: str = "",
) -> None:
    """Two lines saying what just happened to an item.

    `was` is what it was called before, if this changed that: archiving #3 makes it #c, and
    the headline shows both, each in the color of the list it belongs to.
    """
    line = Text.assemble((f"{mark} ", f"bold {style}"), (f"{verb} ", ""))
    here = ARCHIVE if item.archived else ACCENT
    if was:
        there = ACCENT if item.archived else ARCHIVE
        line.append(f"#{was}", style=f"bold {there}").append(" → ", style=FAINT)
        line.append(f"#{item.ref} ", style=f"bold {here}")
    else:
        line.append(f"#{item.ref} ", style=style)
    console.print(line.append_text(_title(item)), no_wrap=True, overflow="ellipsis")
    console.print(_facts(item, *extra), no_wrap=True, overflow="ellipsis")


def renumbered(console: Console, state: State, moves: list[tuple[int, int]]) -> None:
    """Say what the items in a list are called now that others have left it.

    `moves` pairs each changed place with its new one. Neighbours that moved together are
    reported together: "#3 to #6 are now #2 to #5".
    """
    if not moves:
        return
    name = letters if state is State.ARCHIVED else str
    what = "Archive relettered" if state is State.ARCHIVED else "Queue renumbered"

    runs: list[list[tuple[int, int]]] = []
    for move in sorted(moves):
        if runs and move == (runs[-1][-1][0] + 1, runs[-1][-1][1] + 1):
            runs[-1].append(move)
        else:
            runs.append([move])

    def span(run: list[tuple[int, int]], side: int) -> str:
        first, last = name(run[0][side]), name(run[-1][side])
        return f"#{first}" if first == last else f"#{first} to #{last}"

    if len(runs) > MOST_RUNS:
        kind = "letters" if state is State.ARCHIVED else "numbers"
        told = f"{len(moves)} others have new {kind}"
    else:
        told = "; ".join(
            f"{span(run, 0)} {'is' if len(run) == 1 else 'are'} now {span(run, 1)}" for run in runs
        )
    console.print(Text(f"  {what}: {told}.", style=FAINT))


def address(console: Console, item: Item) -> None:
    """An item's link on a line of its own and never cut short, for when the item is gone."""
    console.print(Text(f"  {item.url}", style=FAINT), overflow="fold")


def added(console: Console, item: Item, queue_size: int) -> None:
    saved = ["PDF saved"] if item.file else []
    receipt(console, "Added", item, *saved, f"{queue_size:,} in your queue")


def duplicate(console: Console, item: Item, *, now: datetime | None = None) -> None:
    """Say that a link is one you have, what's become of it since, and how to get back to it."""
    told = [f"Added {_when(item.added_at, now)}"]
    if item.archived:
        style, mark = ARCHIVE, "◆"
        told.append(f"archived {_when(item.archived_at or item.added_at, now)}")
        again = (
            f"[bold]really show {item.ref}[/] reads your copy"
            if item.words
            else f"[bold]really open {item.ref}[/] opens it"
        )
        again += f", [bold]really requeue {item.ref}[/] puts it back in your queue."
    else:
        style, mark = ACCENT, "◇"
        if item.opened_at:
            told.append(f"opened {_when(item.opened_at, now)}")
            again = f"[bold]really open {item.ref}[/] opens it again."
        else:
            told.append("not opened yet")
            again = f"[bold]really open {item.ref}[/] opens it."
        if item.expires_at:
            told.append(f"{left(item.expires_at, now)} before it expires")
    receipt(console, "Already have", item, style=style, mark=mark)
    history = Text(f"  {', '.join(told)}. ", style=FAINT)
    if item.archived and item.note:
        # On a line of its own, and as plain text: it's yours, and may be long.
        console.print(history.append(f"Your note: {item.note}"), no_wrap=True, overflow="ellipsis")
        history = Text("  ", style=FAINT)
    console.print(history.append_text(Text.from_markup(again)))


def timed(console: Console, reading: Reading, pace: Pace) -> None:
    """The line under a receipt saying how long that read took, and where it leaves your pace."""
    line = f"  Read in {took(reading.seconds)}, at {round(reading.wpm)} words a minute. "
    if pace.measured:
        line += f"Your pace is {pace.wpm}, going by {plural(pace.readings, 'timed read')}."
    else:
        line += f"That's {pace.readings} of the {NEEDED} timed reads needed to work out your pace."
    console.print(Text(line, style=FAINT))


def note(console: Console, message: str) -> None:
    """A dim aside under a receipt. `message` may contain markup."""
    console.print(Text.from_markup(f"  {message}", style=STALE))


def error(
    console: Console,
    message: str,
    *,
    detail: str | None = None,
    hint: str | None = None,
):
    console.print(Text.assemble(("✗ ", f"bold {ERROR}"), (message, "bold")))
    if detail:
        console.print(Text(f"  {detail}", style=FAINT))
    if hint:
        console.print(Text.from_markup(f"  {hint}"))


def candidates(console: Console, items: list[Item], limit: int = 8) -> None:
    """The items an ambiguous reference could have meant."""
    for item in items[:limit]:
        line = Text.assemble((f"  #{item.ref} ", ARCHIVE if item.archived else ACCENT), item.name)
        console.print(line, no_wrap=True, overflow="ellipsis")
    if len(items) > limit:
        console.print(Text(f"  …and {len(items) - limit} more", style=FAINT))


def source_line(origin: str, links: list[str], width: int) -> Text:
    line = Text.assemble(("◇ ", ACCENT), (origin, f"bold {ACCENT}"), (" · ", FAINT))
    line.append(links[0] if len(links) == 1 else plural(len(links), "link"), style=FAINT)
    line.truncate(width, overflow="ellipsis")
    return line


def progress_message(verb: str, url: str) -> Text:
    return Text.assemble((verb, ACCENT), (f" · {url}", FAINT))


# ── One item ──────────────────────────────────────────────────────────────────


def card(item: Item, width: int, *, now: datetime | None = None) -> Panel:
    """Everything known about an item, in a box."""
    color = ARCHIVE if item.archived else ACCENT
    facts = [byline(item), reading_time(item)]
    if item.published:
        facts.append(f"published {item.published}")
    history = [f"added {_when(item.added_at, now)}"]
    if item.archived and item.archived_at:
        history.append(f"archived {_when(item.archived_at, now)}")
    elif item.opened_at:
        history.append(f"opened {_when(item.opened_at, now)}")
    lines: list[Text] = [
        Text(item.name, style="bold"),
        Text(" · ".join(fact for fact in facts if fact), style=FAINT),
    ]
    if item.summary:
        lines += [Text(), Text(item.summary)]
    if item.note:
        lines += [Text(), Text.assemble(("Note  ", f"bold {MATCH}"), item.note)]
    if item.tags:
        lines += [Text(), _tags(item)]
    lines += [
        Text(),
        Text(item.url, style=f"underline {color} link {item.url}", no_wrap=True),
    ]
    if item.paywalled:
        lines.append(Text("Paywalled: only the free preview is saved.", style=STALE))
    return Panel(
        Group(*lines),
        box=box.ROUNDED,
        border_style=color,
        padding=(1, 2),
        title=Text(f"#{item.ref}", style=f"bold {color}"),
        title_align="left",
        subtitle=Text(" · ".join(history), style=FAINT),
        subtitle_align="right",
        width=width,
    )


def _dated(moment: datetime, now: datetime | None) -> Text:
    """A moment as your own clock showed it, and how long ago that was."""
    local = moment.astimezone()
    text = Text(f"{local.day} {local:%B %Y}, {local:%H:%M}")
    return text.append(f" · {_when(moment, now)}", style=FAINT)


def details(
    console: Console, item: Item, *, file: Path | None = None, now: datetime | None = None
) -> None:
    """An item's card, and under it what the card leaves out: its saved copy and its history."""
    width = min(_width(console), READING_WIDTH)
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style=FAINT, no_wrap=True)
    grid.add_column()
    if item.words:
        copy = Text(f"{item.words:,} words")
        if item.paywalled:
            copy.append(", the free preview only", style=STALE)
        if item.fetched_at:
            copy.append(f" · fetched {_when(item.fetched_at, now)}", style=FAINT)
    elif item.file:
        copy = Text("no text could be read from the PDF", style=STALE)
    else:
        copy = Text.from_markup(f"none · [bold]really refresh {item.ref}[/] fetches one")
    grid.add_row("Saved copy", copy)
    if file:
        megabytes = file.stat().st_size / 1_000_000
        where = Text(str(file).replace(str(Path.home()), "~", 1), overflow="fold")
        where.append(f" · {plural(item.pages, 'page')} · {megabytes:.1f} MB", style=FAINT)
        grid.add_row("File", where)
    elif item.file:
        gone = f"{item.file} is missing · [bold]really refresh {item.ref}[/] fetches it again"
        grid.add_row("File", Text.from_markup(gone, style=STALE))
    grid.add_row("Added", _dated(item.added_at, now))
    if item.opened_at:
        grid.add_row("Opened", _dated(item.opened_at, now))
    if item.archived_at:
        grid.add_row("Archived", _dated(item.archived_at, now))
    if item.expires_at:
        local = item.expires_at.astimezone()
        expiry = Text(f"{local.day} {local:%B %Y}, {local:%H:%M}", style=STALE)
        expiry.append(f" · {left(item.expires_at, now)}, unless you've read it", style=FAINT)
        grid.add_row("Expires", expiry)
    console.print()
    console.print(card(item, width, now=now))
    console.print(Padding(grid, (0, 0, 0, 3)), width=width)
    console.print()


def article(console: Console, item: Item, content: str) -> None:
    """An item's card, then its saved copy."""
    width = min(_width(console), READING_WIDTH)
    console.print()
    console.print(card(item, width))
    console.print()
    if content:
        console.print(Padding(Markdown(content, hyperlinks=True), (0, 2)), width=width)
    else:
        message = "No saved copy. [bold]really refresh {id}[/] fetches one."
        console.print(Text.from_markup("  " + message.format(id=item.ref), style=FAINT))
    console.print()


def summary(console: Console, item: Item, text: str) -> None:
    """A summary of an item, under a line saying which item it is."""
    width = min(_width(console), READING_WIDTH)
    color = ARCHIVE if item.archived else ACCENT
    heading = Text.assemble((f"#{item.ref} ", f"bold {color}")).append_text(_title(item))
    console.print()
    console.print(heading, no_wrap=True, overflow="ellipsis")
    console.print(_facts(item), no_wrap=True, overflow="ellipsis")
    console.print()
    console.print(Padding(Markdown(text), (0, 2)), width=width)
    console.print()


# ── Search ────────────────────────────────────────────────────────────────────


def hits(console: Console, results: list[Hit], query: str, *, now: datetime | None = None) -> None:
    width = _width(console)
    console.print()
    if not results:
        console.print(Text.assemble(("◇ ", FAINT), ("Nothing matches ", "bold"), (query, MATCH)))
        console.print()
        return
    for hit in results:
        item = hit.item
        color = ARCHIVE if item.archived else ACCENT
        if item.archived:
            status = f"archived {_when(item.archived_at or item.added_at, now)}"
        else:
            status = f"in your queue, added {_when(item.added_at, now)}"
        heading = Text.assemble((f"#{item.ref} ", f"bold {color}")).append_text(
            _marked(hit.title, "bold")
        )
        console.print(heading, width=width, no_wrap=True, overflow="ellipsis")
        details = " · ".join(part for part in (byline(item), status) if part)
        console.print(Text(f"  {details}", style=FAINT), no_wrap=True, overflow="ellipsis")
        if hit.note:
            noted = Text.assemble(("Note  ", f"bold {MATCH}")).append_text(_marked(hit.note))
            console.print(Padding(noted, (0, 0, 0, 2)), width=width)
        if hit.snippet:
            snippet = _marked(" ".join(hit.snippet.split()))
            console.print(Padding(snippet, (0, 0, 0, 2)), width=width)
        console.print()
    console.print(
        Text.assemble(
            ("◇ ", ACCENT),
            (plural(len(results), "match"), "bold"),
            (" for ", FAINT),
            (query, MATCH),
        )
    )
    console.print()


# ── Stats ─────────────────────────────────────────────────────────────────────


def _tally(pairs: list[tuple[str, int]]) -> Text:
    text = Text()
    for i, (name, count) in enumerate(pairs):
        # Non-breaking spaces, so a line can end between entries but not inside one.
        text.append("   " if i else "").append(name.replace(" ", "\u00a0"))
        text.append(f"\u00a0{count}", style=FAINT)
    return text


def stats(console: Console, numbers: Stats, path: str, *, now: datetime | None = None) -> None:
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style=FAINT, no_wrap=True)
    grid.add_column()

    queue = Text(plural(numbers.queued, "thing") + " to read", style="bold")
    if numbers.queued_minutes:
        queue.append(f" · about {duration(numbers.queued_minutes)}", style=FAINT)
    if numbers.unsized:
        queue.append(f" · {numbers.unsized} of unknown length", style=FAINT)
    grid.add_row(Text("Queue", style=ACCENT), queue)
    if oldest := numbers.oldest:
        waiting = Text.assemble((f"#{oldest.ref} ", ACCENT), oldest.name)
        waiting.append(f" · added {_when(oldest.added_at, now)}", style=_age_style(oldest, now))
        grid.add_row("Oldest", waiting)

    archive = Text(plural(numbers.archived, "thing") + " kept", style="bold")
    archive.append(f" · {plural(numbers.archived_words, 'word')} to search", style=FAINT)
    grid.add_row(Text("Archive", style=ARCHIVE), archive)
    grid.add_row(
        "This week",
        f"{numbers.added_lately} added · {numbers.archived_lately} archived",
    )
    speed = Text(f"{numbers.pace.wpm} words a minute")
    if numbers.pace.measured:
        speed.append(f" · going by {plural(numbers.pace.readings, 'timed read')}", style=FAINT)
    else:
        so_far = f"{numbers.pace.readings} of {NEEDED} timed reads so far"
        speed.append(f" · the default until your own is known ({so_far})", style=FAINT)
    grid.add_row("Pace", speed)
    if numbers.sites:
        grid.add_row("Most from", _tally(numbers.sites))
    if numbers.tags:
        grid.add_row("Tags", _tally([(f"#{tag}", count) for tag, count in numbers.tags]))
    grid.add_row("Stored in", Text(path, style=FAINT, overflow="fold"))
    console.print()
    console.print(grid, width=_width(console))
    console.print()


# ── Pace ──────────────────────────────────────────────────────────────────────


def pace(
    console: Console,
    current: Pace,
    readings: list[Reading],
    *,
    now: datetime | None = None,
    limit: int = 12,
) -> None:
    """Your reading pace, and the timed reads it comes from (newest first)."""
    console.print()
    if current.measured:
        headline = f"You read about {current.wpm} words a minute"
        aside = (current.readings - current.counted) // 2
        ends = (
            "the fastest and slowest" if aside == 1 else f"the {aside} fastest and {aside} slowest"
        )
        basis = (
            f"Going by your last {plural(current.readings, 'timed read')}, with {ends} set aside."
        )
    else:
        headline = f"Reading times assume {current.wpm} words a minute for now"
        basis = f"{current.readings} of the {NEEDED} timed reads needed to work out your own pace."
    console.print(Text.assemble(("◇ ", ACCENT), (headline, "bold")))
    console.print(Text(f"  {basis}", style=FAINT))

    if readings:
        kept = middle(readings) if current.measured else readings
        counted = {id(reading) for reading in kept}
        slowest_kept = min(reading.wpm for reading in kept)
        table = Table(box=None, padding=(0, 1), header_style=FAINT)
        table.add_column("When", justify="right", style=FAINT)
        table.add_column("Words", justify="right")
        table.add_column("Took", justify="right")
        table.add_column("Pace", justify="right")
        table.add_column("")
        for reading in readings[:limit]:
            if id(reading) in counted:
                style, remark = "", ""
            else:
                style = FAINT
                remark = (
                    "slowest, set aside" if reading.wpm <= slowest_kept else "fastest, set aside"
                )
            table.add_row(
                ago(reading.finished_at, now),
                Text(f"{reading.words:,}", style=style),
                Text(took(reading.seconds), style=style),
                Text(f"{round(reading.wpm)}", style=style or f"bold {ACCENT}"),
                Text(remark, style=FAINT),
            )
        console.print()
        console.print(table)
        if len(readings) > limit:
            console.print(Text(f"   …and {len(readings) - limit} before those", style=FAINT))

    console.print()
    explanation = (
        "A read is timed from [bold]really next[/] or [bold]really open[/] until you archive or "
        f"delete it. Timings under {SLOWEST_WPM} or over {FASTEST_WPM} words a minute aren't "
        f"kept, and nor are pieces under {SHORTEST_WORDS} words or paywalled previews."
    )
    console.print(
        Padding(Text.from_markup(explanation, style=FAINT), (0, 0, 0, 2)), width=_width(console)
    )
    console.print()


# ── Expiry ────────────────────────────────────────────────────────────────────


def _waiting(queue: list[Item], site: str, rules: dict[str, int]) -> list[Item]:
    """The queued items a site's rule covers."""
    return [item for item in queue if covering(urls.host(item.url), rules) == site]


def expiries(
    console: Console, rules: dict[str, int], queue: list[Item], *, now: datetime | None = None
) -> None:
    """The sites whose unread things get deleted, how long each is given, and what's waiting."""
    console.print()
    if not rules:
        console.print(Text.assemble(("◇ ", ACCENT), ("Nothing expires.", "bold")))
        hint = (
            "  [bold]really expire SITE 3d[/] has anything from a site deleted if it's still "
            "unread after three days."
        )
        console.print(Text.from_markup(hint, style=FAINT))
        console.print()
        return
    console.print(
        Text.assemble(("◇ ", ACCENT), ("Unread things from these sites are deleted", "bold"))
    )
    table = Table(box=None, padding=(0, 1), show_header=False)
    table.add_column()
    table.add_column(style="bold")
    table.add_column(style=FAINT)
    for site, seconds in rules.items():
        waiting = _waiting(queue, site, rules)
        soonest = min((item.expires_at for item in waiting if item.expires_at), default=None)
        note = f"{len(waiting)} waiting, the next has {left(soonest, now)}" if soonest else ""
        table.add_row(site, f"after {span(seconds)}", note)
    console.print(Padding(table, (0, 0, 0, 1)))
    console.print()


def expiry_set(
    console: Console,
    site: str,
    rules: dict[str, int],
    queue: list[Item],
    *,
    now: datetime | None = None,
) -> None:
    """Confirm a rule, and say what it means for anything already waiting."""
    told = f"Unread things from {site} will be deleted after {span(rules[site])}."
    console.print(Text.assemble(("✓ ", f"bold {ACCENT}"), (told, "bold")))
    waiting = [(item.ref, item.expires_at) for item in _waiting(queue, site, rules)]
    if waiting:
        each = ", ".join(f"#{ref} ({left(due, now)})" for ref, due in waiting[:6] if due)
        more = f" and {len(waiting) - 6} more" if len(waiting) > 6 else ""
        console.print(Text(f"  Waiting now: {each}{more}.", style=FAINT))
