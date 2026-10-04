"""Sites whose posts go stale: how long an unread one is kept, and which rule covers a link."""

from __future__ import annotations

import re

from . import urls

UNITS = {"h": 3600, "d": 86400, "w": 604800}
_SPAN = re.compile(r"(\d+(?:\.\d+)?)\s*(h|hrs?|hours?|d|days?|w|wks?|weeks?)?", re.IGNORECASE)


def parse_span(text: str) -> int:
    """How many seconds "3d", "12h" or "2 weeks" comes to. A bare number is days."""
    match = _SPAN.fullmatch(text.strip())
    if not match:
        raise ValueError(text)
    seconds = round(float(match.group(1)) * UNITS[(match.group(2) or "d")[0].lower()])
    if seconds <= 0:
        raise ValueError(text)
    return seconds


def site_of(text: str) -> str:
    """The site meant by "thezvi.substack.com", "www.thezvi.substack.com" or a link to a post."""
    found = urls.find_urls(text)
    return urls.host(found[0]) if found else ""


def covering(host: str, rules: dict[str, int]) -> str | None:
    """The site whose rule covers this host: its own if it has one, or else its nearest parent's.

    A rule for example.com covers news.example.com, unless that has a rule of its own.
    """
    labels = host.split(".")
    for start in range(len(labels)):
        site = ".".join(labels[start:])
        if site in rules:
            return site
    return None


def rule_for(host: str, rules: dict[str, int]) -> int | None:
    """How long something unread from this host is kept, in seconds, if any rule covers it."""
    site = covering(host, rules)
    return rules[site] if site else None
