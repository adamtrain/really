"""Everything you see: the queue, receipts, search results and the saved copy of an article."""

from __future__ import annotations

from datetime import UTC, datetime

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .pace import FASTEST_WPM, NEEDED, SHORTEST_WORDS, SLOWEST_WPM, Pace, Reading, middle
from .store import MARK, UNMARK, Hit, Item, Stats

MAX_WIDTH = 110
READING_WIDTH = 88

ACCENT = "#4cc2a8"
ARCHIVE = "#9d8cf7"
MATCH = "#f5c451"
STALE = "#eb9a12"
ERROR = "#f2506e"
FAINT = "grey42"

# How long something can sit in the queue before its age starts to stand out.
STALE_DAYS = 30
ANCIENT_DAYS = 90


def _width(console: Console) -> int:
    return min(console.width, MAX_WIDTH)


def plural(n: int, word: str) -> str:
    ending = "" if n == 1 else "es" if word.endswith(("ch", "sh", "s", "x")) else "s"
    return f"{n:,} {word}{ending}"


def ago(moment: datetime, now: datetime | None = None) -> str:
    """A compact age: 5m, 3h, 2d, 6w, 4mo, 2y."""
    seconds = ((now or datetime.now(UTC)) - moment).total_seconds()
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


def duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def took(seconds: int) -> str:
    """How long a read took: 45 s, 6 min, 1 h 5 min."""
    return f"{seconds} s" if seconds < 90 else duration(round(seconds / 60))


def reading_time(item: Item) -> str:
    """How long an item takes to read. A + means at least: we only have the free preview."""
    if not item.words:
        return ""
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
    days = ((now or datetime.now(UTC)) - item.added_at).days
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
        table.add_row(
            _number(item, mixed=not archived),
            _title(item),
            byline(item),
            reading_time(item) or Text("—", style=FAINT),
            Text(ago(moment, now), style=_age_style(item, now)),
        )
    console.print()
    console.print(table)


def _number(item: Item, mixed: bool) -> Text:
    """An item's id, with a mark if it's one you've opened (or, among queued ones, archived)."""
    color = ARCHIVE if item.archived else ACCENT
    mark = ("◆ " if mixed else "") if item.archived else "▸ " if item.opened_at else ""
    return Text.assemble((mark, color), (str(item.id), f"bold {color}"))


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
    parts.append(f"oldest added {_when(items[0].added_at, now)}")
    console.print(
        Text.assemble(("◇ ", ACCENT), (parts[0], "bold"), (" · ", FAINT)).append(
            Text(" · ".join(parts[1:]), style=FAINT)
        )
    )
    started = [item for item in items if item.opened_at]
    hint = (
        f"  [{ACCENT}]▸[/] opened: [bold]really archive[/] or [bold]really delete[/] "
        "when you're done"
        if started
        else "  [bold]really next[/] opens the one at the top"
    )
    console.print(Text.from_markup(hint, style=FAINT))
    console.print()


def archive_summary(console: Console, items: list[Item]) -> None:
    console.print()
    if not items:
        console.print(Text.assemble(("◆ ", ARCHIVE), ("Nothing archived yet.", "bold")))
        console.print(Text.from_markup("  [bold]really archive[/] keeps what you've read."))
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
) -> None:
    line = Text.assemble((f"{mark} ", f"bold {style}"), (f"{verb} ", ""), (f"#{item.id} ", style))
    console.print(line.append_text(_title(item)), no_wrap=True, overflow="ellipsis")
    console.print(_facts(item, *extra), no_wrap=True, overflow="ellipsis")


def added(console: Console, item: Item, queue_size: int) -> None:
    receipt(console, "Added", item, f"{queue_size:,} in your queue")


def duplicate(console: Console, item: Item) -> None:
    if item.archived:
        where = f"archived {_when(item.archived_at or item.added_at)}"
        style, mark = ARCHIVE, "◆"
    else:
        where, style, mark = f"added {_when(item.added_at)}", ACCENT, "◇"
    receipt(console, "Already have", item, where, style=style, mark=mark)


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
        line = Text.assemble((f"  #{item.id} ", ARCHIVE if item.archived else ACCENT), item.name)
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
        title=Text(f"#{item.id}", style=f"bold {color}"),
        title_align="left",
        subtitle=Text(" · ".join(history), style=FAINT),
        subtitle_align="right",
        width=width,
    )


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
        console.print(Text.from_markup("  " + message.format(id=item.id), style=FAINT))
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
        heading = Text.assemble((f"#{item.id} ", f"bold {color}")).append_text(
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
        waiting = Text.assemble((f"#{oldest.id} ", ACCENT), oldest.name)
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
