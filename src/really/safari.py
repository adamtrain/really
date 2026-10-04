"""Read Safari's Reading List straight out of its bookmarks file."""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

BOOKMARKS = Path.home() / "Library" / "Safari" / "Bookmarks.plist"
READING_LIST = "com.apple.ReadingList"


class SafariError(Exception):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True, slots=True)
class Entry:
    url: str
    title: str
    preview: str
    added: datetime | None
    read: bool


def _entry(child: dict) -> Entry | None:
    url = child.get("URLString", "")
    if not url.startswith(("http://", "https://")):
        return None
    details = child.get("ReadingList", {})
    title = child.get("URIDictionary", {}).get("title", "").strip()
    added = details.get("DateAdded")
    return Entry(
        url=url,
        title="" if title == url else title,  # Safari uses the URL when it never got a title
        preview=" ".join(details.get("PreviewText", "").split()),
        added=added.replace(tzinfo=UTC) if isinstance(added, datetime) else None,
        read="DateLastViewed" in details,
    )


def reading_list(path: Path = BOOKMARKS) -> list[Entry]:
    """Everything on Safari's Reading List, oldest first."""
    try:
        with path.open("rb") as file:
            bookmarks = plistlib.load(file)
    except FileNotFoundError as e:
        raise SafariError(f"There's no Safari bookmarks file at {path}.") from e
    except PermissionError as e:
        raise SafariError(
            "macOS won't let this terminal read Safari's bookmarks.",
            hint="Give your terminal app Full Disk Access (System Settings → Privacy & Security "
            "→ Full Disk Access), restart it, and try again.",
        ) from e
    except (plistlib.InvalidFileException, ValueError, OSError) as e:
        raise SafariError(f"Couldn't read {path}: {e}") from e
    for folder in bookmarks.get("Children", []):
        if folder.get("Title") == READING_LIST:
            entries = [entry for child in folder.get("Children", []) if (entry := _entry(child))]
            return sorted(
                entries,
                key=lambda entry: entry.added or datetime.min.replace(tzinfo=UTC),
            )
    return []
