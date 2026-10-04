"""Take your reading list elsewhere: Markdown files with front matter, or plain dicts for JSON."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from .store import Item


def to_dict(item: Item, content: str | None = None) -> dict:
    """An item as JSON-ready data. Pass `content` to include the saved copy."""
    data = {
        "id": item.id,
        "url": item.url,
        "title": item.name,
        "author": item.author,
        "site": item.site or item.host,
        "published": item.published or None,
        "summary": item.summary,
        "tags": list(item.tags),
        "note": item.note,
        "state": str(item.state),
        "words": item.words,
        "minutes": item.minutes,
        "paywalled": item.paywalled,
        "added_at": item.added_at.isoformat(),
        "opened_at": item.opened_at.isoformat() if item.opened_at else None,
        "archived_at": item.archived_at.isoformat() if item.archived_at else None,
    }
    if content is not None:
        data["content"] = content
    return data


def _day(moment: datetime) -> str:
    """The date as you'd have seen it on your own calendar."""
    return moment.astimezone().date().isoformat()


def slug(text: str, length: int = 60) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return words[:length].rstrip("-") or "untitled"


def filename(item: Item) -> str:
    """Stable for an item, so exporting again overwrites rather than duplicates."""
    return f"{slug(item.name)}-{item.id}.md"


def markdown(item: Item, content: str) -> str:
    """An item as a Markdown document, with its details as YAML front matter."""
    details = {
        "title": item.name,
        "author": item.author,
        "site": item.site or item.host,
        "url": item.url,
        "published": item.published,
        "added": _day(item.added_at),
        "archived": _day(item.archived_at) if item.archived_at else "",
        "tags": list(item.tags),
    }
    # JSON strings and lists are valid YAML, and JSON's quoting is the unambiguous kind.
    front = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in details.items()]
    parts = ["---", *front, "---", "", f"# {item.name}", ""]
    if item.note:
        parts += [*(f"> {line}" for line in item.note.splitlines()), ""]
    parts += [content.strip(), ""]
    return "\n".join(parts)


def write_markdown(directory: Path, entries: list[tuple[Item, str]]) -> list[Path]:
    """Write one file per item into `directory`, creating it if need be."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for item, content in entries:
        path = directory / filename(item)
        path.write_text(markdown(item, content), encoding="utf-8")
        written.append(path)
    return written
