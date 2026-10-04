"""The reading list itself: one SQLite file, with a full-text index over everything in it."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from . import urls
from .extract import Article
from .pace import RECENT, Pace, Reading, estimate, reading_minutes

ENV_DB = "REALLY_DB"
FILENAME = "really.db"

FIELDS = (
    "id",
    "number",
    "url",
    "title",
    "author",
    "site",
    "published",
    "summary",
    "tags",
    "note",
    "words",
    "paywalled",
    "state",
    "added_at",
    "opened_at",
    "archived_at",
    "fetched_at",
)
COLUMNS = ", ".join(f"items.{field}" for field in FIELDS)

# The search index mirrors these columns of `items`, and the triggers keep the two in step.
INDEXED = ("title", "author", "site", "tags", "note", "text")
WEIGHTS = (10.0, 5.0, 3.0, 6.0, 6.0, 1.0)

SCHEMA = f"""
CREATE TABLE items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    site        TEXT NOT NULL DEFAULT '',
    published   TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    words       INTEGER NOT NULL DEFAULT 0,
    paywalled   INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'archived')),
    added_at    TEXT NOT NULL,
    opened_at   TEXT,
    archived_at TEXT,
    fetched_at  TEXT
);
CREATE INDEX items_state ON items (state, added_at);

CREATE VIRTUAL TABLE items_fts USING fts5(
    {", ".join(INDEXED)},
    content='items', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts (rowid, {", ".join(INDEXED)})
    VALUES (new.id, {", ".join(f"new.{c}" for c in INDEXED)});
END;
CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, {", ".join(INDEXED)})
    VALUES ('delete', old.id, {", ".join(f"old.{c}" for c in INDEXED)});
END;
CREATE TRIGGER items_au AFTER UPDATE OF {", ".join(INDEXED)} ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, {", ".join(INDEXED)})
    VALUES ('delete', old.id, {", ".join(f"old.{c}" for c in INDEXED)});
    INSERT INTO items_fts (rowid, {", ".join(INDEXED)})
    VALUES (new.id, {", ".join(f"new.{c}" for c in INDEXED)});
END;
"""

# Timed reads, kept apart from the items so they outlast the ones you delete.
READINGS = """
CREATE TABLE readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    words       INTEGER NOT NULL,
    seconds     INTEGER NOT NULL,
    finished_at TEXT NOT NULL
);
"""

# What you call an item by is its place in its list, kept separate from `id` so that closing
# a gap is a matter of renumbering and never of re-indexing anyone's text. Existing items are
# numbered in the order they joined the queue, or the archive.
NUMBERS = """
ALTER TABLE items ADD COLUMN number INTEGER NOT NULL DEFAULT 0;
UPDATE items SET number = (
    SELECT count(*) FROM items AS other
    WHERE other.state = items.state
      AND (coalesce(other.archived_at, other.added_at), other.id)
       <= (coalesce(items.archived_at, items.added_at), items.id)
);
CREATE UNIQUE INDEX items_number ON items (state, number);
"""

# Each script takes the file from one version to the next; a new file runs them all.
MIGRATIONS = (SCHEMA, READINGS, NUMBERS)
SCHEMA_VERSION = len(MIGRATIONS)

# The next free place in a list: one past its last.
NEXT = "(SELECT coalesce(max(number), 0) + 1 FROM items WHERE state = :state)"
# Letters beyond this aren't a place in anyone's archive, just a word.
LONGEST_CODE = 6

# What you can put before a colon in a search: `author:sutton`, `tag:ml`.
SEARCH_FIELDS = {
    "title": "title",
    "author": "author",
    "by": "author",
    "site": "site",
    "tag": "tags",
    "tags": "tags",
    "note": "note",
    "text": "text",
}
_TERM = re.compile(r'(?:(\w+):)?(?:"([^"]*)"|(\S+))')

# Wrapped around matches in search results; see Hit.
MARK, UNMARK = "\x02", "\x03"


class StoreError(Exception):
    pass


class NotFound(StoreError):
    pass


class Ambiguous(StoreError):
    def __init__(self, ref: str, matches: list[Item]):
        super().__init__(f'"{ref}" matches {len(matches)} things.')
        self.matches = matches


class State(StrEnum):
    QUEUED = "queued"
    ARCHIVED = "archived"


def letters(number: int) -> str:
    """A number as letters, the way spreadsheets name columns: a, b, … z, aa, ab, …"""
    code = ""
    while number > 0:
        number, last = divmod(number - 1, 26)
        code = chr(ord("a") + last) + code
    return code


def from_letters(code: str) -> int:
    number = 0
    for letter in code.lower():
        number = number * 26 + ord(letter) - ord("a") + 1
    return number


@dataclass(frozen=True, slots=True)
class Item:
    id: int  # permanent, and never shown: what the rest of the database knows it by
    number: int  # its place in the queue or in the archive, which changes as others leave
    url: str
    title: str
    author: str
    site: str
    published: str
    summary: str
    tags: tuple[str, ...]
    note: str
    words: int
    minutes: int  # how long it takes to read, at your pace
    paywalled: bool
    state: State
    added_at: datetime
    opened_at: datetime | None
    archived_at: datetime | None
    fetched_at: datetime | None

    @property
    def ref(self) -> str:
        """What you call it: a number while it's in the queue, letters once it's archived."""
        return letters(self.number) if self.archived else str(self.number)

    @property
    def name(self) -> str:
        """The title, or the best stand-in for one."""
        return self.title or urls.title_from_url(self.url)

    @property
    def host(self) -> str:
        return urls.host(self.url)

    @property
    def archived(self) -> bool:
        return self.state is State.ARCHIVED


@dataclass(frozen=True, slots=True)
class Hit:
    """A search result, with MARK…UNMARK around the words that matched."""

    item: Item
    title: str
    snippet: str  # the most relevant stretch of the text, or its opening lines
    note: str = ""  # your note, if the match was (also) in there


@dataclass(frozen=True, slots=True)
class Stats:
    queued: int
    queued_minutes: int
    unsized: int  # queued items whose length we don't know
    oldest: Item | None
    archived: int
    archived_words: int
    added_lately: int
    archived_lately: int
    sites: list[tuple[str, int]]
    tags: list[tuple[str, int]]
    pace: Pace


def default_path() -> Path:
    """Where the reading list lives: $REALLY_DB, or the platform's usual spot for app data.

    $REALLY_DB can name the file itself, or a folder to keep it in.
    """
    if override := os.environ.get(ENV_DB, "").strip():
        path = Path(override).expanduser()
        return path / FILENAME if path.is_dir() or override.endswith(("/", os.sep)) else path
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "really" / FILENAME


def normalize_tag(tag: str) -> str:
    return re.sub(r"[^\w/]+", "-", tag.strip().lower().lstrip("#")).strip("-")


def normalize_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Lowercase, hyphenated, de-duplicated. `-t "a, b"` and `-t a -t b` mean the same."""
    pieces = (piece for tag in tags for piece in tag.split(","))
    return tuple(dict.fromkeys(tag for tag in map(normalize_tag, pieces) if tag))


def match_expression(query: str) -> str:
    """Translate what someone typed into an FTS5 query that can't be a syntax error.

    Words must all match. "Quoted phrases" match exactly, a trailing * matches prefixes, and
    `field:word` looks in one field only.
    """
    parts = []
    for field, phrase, word in _TERM.findall(query):
        term = phrase or word
        column = SEARCH_FIELDS.get(field.lower())
        if field and column is None:
            term = f"{field}:{term}"  # not a field, just a word with a colon in it
        prefix = not phrase and term.endswith("*")
        if not re.search(r"\w", term):
            continue
        quoted = '"' + term.rstrip("*").replace('"', '""') + '"' + ("*" if prefix else "")
        parts.append(f"{column}: {quoted}" if column else quoted)
    return " ".join(parts)


def is_lesser(article: Article, item: Item) -> bool:
    """Whether a freshly fetched article is a worse copy than the one an item already has.

    A page can lose its text after you saved it: taken down, put behind a paywall, or simply
    refusing us this time. And a copy you pasted in beats the preview the page will give us.
    """
    if not article.text:
        return True
    if article.paywalled and article.words < item.words:
        return True
    return article.words * 2 < item.words


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _stamp(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None


def _moment(stamp: str | None) -> datetime | None:
    return datetime.fromisoformat(stamp) if stamp else None


def _item(row: sqlite3.Row, wpm: int) -> Item:
    return Item(
        id=row["id"],
        number=row["number"],
        url=row["url"],
        title=row["title"],
        author=row["author"],
        site=row["site"],
        published=row["published"],
        summary=row["summary"],
        tags=tuple(row["tags"].split()),
        note=row["note"],
        words=row["words"],
        minutes=reading_minutes(row["words"], wpm),
        paywalled=bool(row["paywalled"]),
        state=State(row["state"]),
        added_at=datetime.fromisoformat(row["added_at"]),
        opened_at=_moment(row["opened_at"]),
        archived_at=_moment(row["archived_at"]),
        fetched_at=_moment(row["fetched_at"]),
    )


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._pace: Pace | None = None
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} was written by a newer version of really. Upgrade to open it."
            )
        for script in MIGRATIONS[version:]:
            self.db.executescript(script)
        if version < SCHEMA_VERSION:
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Reading ───────────────────────────────────────────────────────────────

    def _load(self, row: sqlite3.Row) -> Item:
        return _item(row, self.pace().wpm)

    def get(self, id: int) -> Item | None:
        row = self.db.execute(f"SELECT {COLUMNS} FROM items WHERE id = ?", (id,)).fetchone()
        return self._load(row) if row else None

    def _require(self, id: int) -> Item:
        item = self.get(id)
        if item is None:
            raise NotFound(f"There's no #{id}.")
        return item

    def by_url(self, url: str) -> Item | None:
        row = self.db.execute(f"SELECT {COLUMNS} FROM items WHERE url = ?", (url,)).fetchone()
        return self._load(row) if row else None

    def items(self, state: State | None = None, tag: str | None = None) -> list[Item]:
        """Items in order: the queue from 1, then the archive from a."""
        where, values = self._filters(state, tag)
        rows = self.db.execute(
            f"SELECT {COLUMNS} FROM items WHERE 1 {where} ORDER BY state = 'archived', number",
            values,
        )
        return [self._load(row) for row in rows]

    def numbered(self, state: State, number: int) -> Item | None:
        row = self.db.execute(
            f"SELECT {COLUMNS} FROM items WHERE state = ? AND number = ?", (str(state), number)
        ).fetchone()
        return self._load(row) if row else None

    def count(self, state: State) -> int:
        return self.db.execute("SELECT count(*) FROM items WHERE state = ?", (state,)).fetchone()[0]

    def content(self, id: int) -> str:
        """The saved copy of an item, as Markdown. Empty if there isn't one."""
        row = self.db.execute("SELECT content FROM items WHERE id = ?", (id,)).fetchone()
        return row["content"] if row else ""

    def last_opened(self) -> Item | None:
        """The queued item you opened most recently: the one you're presumably done reading."""
        row = self.db.execute(
            f"SELECT {COLUMNS} FROM items WHERE state = 'queued' AND opened_at IS NOT NULL "
            "ORDER BY opened_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return self._load(row) if row else None

    def resolve(self, ref: str, state: State | None = None) -> Item:
        """Find the one item meant by a number, letters, a URL, or a few words from a title.

        A number is always something in the queue, and letters something in the archive.
        `state` only narrows what words can match.
        """
        ref = ref.strip()
        name = ref.removeprefix("#").lower()
        if name.isdigit():
            if item := self.numbered(State.QUEUED, int(name)):
                return item
            raise NotFound(f"There's no #{name} in your queue.")
        if name.isascii() and name.isalpha() and len(name) <= LONGEST_CODE:
            if item := self.numbered(State.ARCHIVED, from_letters(name)):
                return item
            if len(name) <= 2:  # too short to be a title someone's searching for
                raise NotFound(f"There's no #{name} in your archive.")
        for url in urls.find_urls(ref):
            if item := self.by_url(urls.clean(url)):
                return item
        # Matched here rather than with LIKE, which only ignores case for ASCII.
        needle = ref.casefold()
        matches = [
            item
            for item in self.items(state)
            if needle in item.title.casefold() or needle in item.url.casefold()
        ]
        if not matches:
            where = f" in your {'archive' if state is State.ARCHIVED else 'queue'}" if state else ""
            raise NotFound(f'Nothing{where} matches "{ref}".')
        if len(matches) > 1:
            raise Ambiguous(ref, matches)
        return matches[0]

    def search(
        self,
        query: str,
        state: State | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> list[Hit]:
        """Full-text search, best matches first."""
        match = match_expression(query)
        if not match:
            return []
        where, values = self._filters(state, tag)
        rows = self.db.execute(
            f"""
            SELECT {COLUMNS},
                   highlight(items_fts, 0, char(2), char(3)) AS marked_title,
                   highlight(items_fts, 4, char(2), char(3)) AS marked_note,
                   snippet(items_fts, 5, char(2), char(3), '…', 40) AS marked_text
            FROM items_fts JOIN items ON items.id = items_fts.rowid
            WHERE items_fts MATCH :match {where}
            ORDER BY bm25(items_fts, {", ".join(map(str, WEIGHTS))}), items.id
            LIMIT :limit
            """,
            {"match": match, "limit": limit, **values},
        )
        hits = []
        for row in rows:
            item = self._load(row)
            note = row["marked_note"] if MARK in row["marked_note"] else ""
            snippet = row["marked_text"] or item.summary
            hits.append(Hit(item, row["marked_title"] or item.name, snippet, note))
        return hits

    def stats(self, days: int = 7) -> Stats:
        queue = self.items(State.QUEUED)
        archive = self.items(State.ARCHIVED)
        since = _now() - timedelta(days=days)
        everything = [*queue, *archive]
        return Stats(
            queued=len(queue),
            queued_minutes=sum(item.minutes for item in queue),
            unsized=sum(1 for item in queue if not item.words),
            oldest=min(queue, key=lambda item: item.added_at) if queue else None,
            archived=len(archive),
            archived_words=sum(item.words for item in archive),
            added_lately=sum(1 for item in everything if item.added_at >= since),
            archived_lately=sum(
                1 for item in archive if item.archived_at and item.archived_at >= since
            ),
            sites=Counter(item.site or item.host for item in everything).most_common(5),
            tags=Counter(tag for item in everything for tag in item.tags).most_common(),
            pace=self.pace(),
        )

    def readings(self, limit: int = RECENT) -> list[Reading]:
        """Your latest timed reads, newest first."""
        rows = self.db.execute(
            "SELECT words, seconds, finished_at FROM readings ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            Reading(row["words"], row["seconds"], datetime.fromisoformat(row["finished_at"]))
            for row in rows
        ]

    def pace(self) -> Pace:
        """How fast you read, going by your recent timed reads."""
        if self._pace is None:
            self._pace = estimate(self.readings())
        return self._pace

    @staticmethod
    def _filters(state: State | None, tag: str | None) -> tuple[str, dict[str, str]]:
        where, values = "", {}
        if state is not None:
            where += " AND items.state = :state"
            values["state"] = str(state)
        if tag:
            where += " AND instr(' ' || items.tags || ' ', ' ' || :tag || ' ') > 0"
            values["tag"] = normalize_tag(tag)
        return where, values

    # ── Writing ───────────────────────────────────────────────────────────────

    def add(
        self,
        url: str,
        article: Article | None = None,
        *,
        fetched: bool = False,
        tags: Iterable[str] = (),
        state: State = State.QUEUED,
        added_at: datetime | None = None,
    ) -> Item:
        """Put a link on the list. `fetched` says whether `article` came from the page itself."""
        article = article or Article()
        added = added_at or _now()
        cursor = self.db.execute(
            f"""
            INSERT INTO items (url, title, author, site, published, summary, tags, content, text,
                               words, paywalled, state, added_at, archived_at, fetched_at, number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {NEXT.replace(":state", "?")})
            """,
            (
                url,
                article.title,
                article.author,
                article.site,
                article.published,
                article.summary,
                " ".join(normalize_tags(tags)),
                article.content,
                article.text,
                article.words,
                article.paywalled,
                str(state),
                _stamp(added),
                _stamp(added) if state is State.ARCHIVED else None,
                _stamp(_now()) if fetched else None,
                str(state),
            ),
        )
        self.db.commit()
        assert cursor.lastrowid is not None
        return self._require(cursor.lastrowid)

    def _set(self, id: int, **columns: object) -> Item:
        assignments = ", ".join(f"{column} = :{column}" for column in columns)
        self.db.execute(f"UPDATE items SET {assignments} WHERE id = :id", {"id": id, **columns})
        self.db.commit()
        return self._require(id)

    def set_article(self, id: int, article: Article, url: str | None = None) -> Item:
        """Record a fresh fetch: fill in any metadata that was blank, and replace the saved copy.

        Metadata you already have is left alone, since you may have corrected it by hand. And
        the copy you already have is kept if the new one is a lesser thing (see is_lesser).
        """
        item = self._require(id)
        columns: dict[str, object] = {"fetched_at": _stamp(_now())}
        for field in ("title", "author", "site", "published", "summary"):
            if not getattr(item, field) and getattr(article, field):
                columns[field] = getattr(article, field)
        if not is_lesser(article, item):
            columns |= {
                "content": article.content,
                "text": article.text,
                "words": article.words,
                "paywalled": article.paywalled,
            }
        # Follow the link to wherever it really leads, unless that's already a different item.
        if url and url != item.url and self.by_url(url) is None:
            columns["url"] = url
        return self._set(id, **columns)

    def set_text(self, id: int, text: str) -> Item:
        """Replace the saved copy with text you supply, for pages only your browser can read."""
        return self._set(id, content=text, text=text, words=len(text.split()), paywalled=False)

    def edit(self, id: int, **fields: str) -> Item:
        allowed = {"url", "title", "author", "site", "published", "summary", "note"}
        if unknown := fields.keys() - allowed:
            raise ValueError(f"Can't edit {', '.join(sorted(unknown))}")
        return self._set(id, **fields) if fields else self._require(id)

    def set_tags(self, id: int, tags: Iterable[str]) -> Item:
        return self._set(id, tags=" ".join(normalize_tags(tags)))

    def record(self, reading: Reading) -> None:
        self.db.execute(
            "INSERT INTO readings (words, seconds, finished_at) VALUES (?, ?, ?)",
            (reading.words, reading.seconds, _stamp(reading.finished_at)),
        )
        self.db.commit()
        self._pace = None

    def finish(self, id: int) -> Reading | None:
        """Note that a queued item has just been read, timing it from when it was opened.

        Returns the reading if it was kept. One that couldn't have been a start-to-finish read
        isn't (see Reading.plausible), and nor is a paywalled preview, whose length isn't the
        length of what you read.
        """
        item = self._require(id)
        if item.archived or item.opened_at is None or item.paywalled:
            return None
        now = _now()
        reading = Reading(item.words, int((now - item.opened_at).total_seconds()), now)
        if not reading.plausible:
            return None
        self.record(reading)
        return reading

    def mark_opened(self, id: int) -> Item:
        return self._set(id, opened_at=_stamp(_now()))

    def archive(self, id: int) -> Item:
        """Move an item to the archive, where it takes the next letters. The queue closes up."""
        item = self._require(id)
        if item.archived:
            return item
        return self._move(item, State.ARCHIVED, archived_at=_stamp(_now()))

    def requeue(self, id: int) -> Item:
        """Move an item back to the end of the queue. The archive closes up."""
        item = self._require(id)
        if not item.archived:
            return item
        return self._move(item, State.QUEUED, archived_at=None, opened_at=None)

    def delete(self, id: int) -> None:
        item = self.get(id)
        if item is None:
            return
        self.db.execute("DELETE FROM items WHERE id = ?", (id,))
        self._close_up(item.state, item.number)
        self.db.commit()

    def _move(self, item: Item, state: State, **columns: object) -> Item:
        assignments = "".join(f", {column} = :{column}" for column in columns)
        self.db.execute(
            f"UPDATE items SET state = :state, number = {NEXT}{assignments} WHERE id = :id",
            {"id": item.id, "state": str(state), **columns},
        )
        self._close_up(item.state, item.number)
        self.db.commit()
        return self._require(item.id)

    def _close_up(self, state: State, gap: int) -> None:
        """Move everything after a gap down by one, so a list is always 1, 2, 3… with none missing.

        By way of negative numbers, in two steps. Moved one row at a time, each would land for
        a moment on its neighbour's number, which the index on them forbids.
        """
        self.db.execute(
            "UPDATE items SET number = 1 - number WHERE state = ? AND number > ?", (str(state), gap)
        )
        self.db.execute(
            "UPDATE items SET number = -number WHERE state = ? AND number < 0", (str(state),)
        )
