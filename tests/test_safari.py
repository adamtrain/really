import plistlib
from datetime import UTC, datetime

import pytest

from really import safari


def bookmark(url, title, added, viewed=None, preview=""):
    details = {"DateAdded": added, "PreviewText": preview}
    if viewed:
        details["DateLastViewed"] = viewed
    return {
        "URLString": url,
        "URIDictionary": {"title": title},
        "ReadingList": details,
        "WebBookmarkType": "WebBookmarkTypeLeaf",
    }


@pytest.fixture
def bookmarks(tmp_path):
    reading_list = [
        bookmark(
            "https://example.com/newer", "Newer", datetime(2026, 5, 2, 9), preview="  A  preview. "
        ),
        bookmark("https://example.com/older", "Older", datetime(2026, 1, 15, 8)),
        bookmark(
            "https://example.com/read", "Read", datetime(2026, 3, 1), viewed=datetime(2026, 3, 2)
        ),
        bookmark(
            "https://example.com/untitled", "https://example.com/untitled", datetime(2026, 6, 1)
        ),
        bookmark("file:///Users/me/notes.html", "Local", datetime(2026, 6, 2)),
    ]
    tree = {
        "Children": [
            {"Title": "BookmarksBar", "Children": [{"URLString": "https://example.com/bar"}]},
            {"Title": safari.READING_LIST, "Children": reading_list},
        ]
    }
    path = tmp_path / "Bookmarks.plist"
    with path.open("wb") as file:
        plistlib.dump(tree, file, fmt=plistlib.FMT_BINARY)
    return path


def test_reads_the_reading_list_oldest_first(bookmarks):
    entries = safari.reading_list(bookmarks)
    assert [entry.title for entry in entries] == ["Older", "Read", "Newer", ""]
    older, read, newer, untitled = entries
    assert older.added == datetime(2026, 1, 15, 8, tzinfo=UTC)
    assert newer.preview == "A preview."
    assert read.read and not newer.read
    assert untitled.url == "https://example.com/untitled"


def test_no_reading_list_is_an_empty_one(tmp_path):
    path = tmp_path / "Bookmarks.plist"
    path.write_bytes(plistlib.dumps({"Children": []}))
    assert safari.reading_list(path) == []


def test_missing_file(tmp_path):
    with pytest.raises(safari.SafariError, match="no Safari bookmarks"):
        safari.reading_list(tmp_path / "nope.plist")


def test_unreadable_file_explains_full_disk_access(tmp_path, monkeypatch):
    path = tmp_path / "Bookmarks.plist"

    def denied(*args, **kwargs):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(type(path), "open", denied)
    with pytest.raises(safari.SafariError) as e:
        safari.reading_list(path)
    assert "Full Disk Access" in (e.value.hint or "")


def test_garbage_file(tmp_path):
    path = tmp_path / "Bookmarks.plist"
    path.write_bytes(b"not a plist")
    with pytest.raises(safari.SafariError, match="Couldn't read"):
        safari.reading_list(path)
