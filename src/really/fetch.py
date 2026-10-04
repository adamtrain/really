"""Download a page, following whatever redirects stand between a link and the article."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

# Plenty of sites turn away anything that doesn't look like a browser.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Safari/605.1.15"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = httpx.Timeout(20, connect=10)
MAX_BYTES = 25 * 1024 * 1024

# Some redirects are a tiny page that forwards you with a <meta refresh> instead of an HTTP
# status. Substack's emailed links work this way, as do a lot of newsletter click-trackers.
MAX_HOPS = 5
INTERSTITIAL_BYTES = 16 * 1024
_META = re.compile(rb"<meta\b[^>]*>", re.IGNORECASE)
_IS_REFRESH = re.compile(rb"http-equiv\s*=\s*[\"']?refresh", re.IGNORECASE)
_REFRESH_URL = re.compile(rb"url\s*=\s*[\"']?([^\"'>\s]+)", re.IGNORECASE)

# The "comment" button in a Substack email opens a view of the post that holds every comment
# and none of the post.
_COMMENTS_VIEW = re.compile(r"(/p/[^/]+)/comments/?")
_SUBSTACK = b"substackcdn.com"


class FetchError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Page:
    url: str  # where the redirects ended up
    content_type: str
    body: bytes
    charset: str | None = None  # only when the server declared one


def new_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers=HEADERS, timeout=TIMEOUT, follow_redirects=True, transport=transport
    )


def _forwarding_address(page: Page) -> str | None:
    """Where to go from this page to reach the article, if this page isn't it."""
    if "html" not in page.content_type:
        return None
    if len(page.body) <= INTERSTITIAL_BYTES:
        for tag in _META.findall(page.body):
            if _IS_REFRESH.search(tag) and (target := _REFRESH_URL.search(tag)):
                address = html.unescape(target.group(1).decode("utf-8", errors="replace"))
                return urljoin(page.url, address)
    parts = urlsplit(page.url)
    if (view := _COMMENTS_VIEW.fullmatch(parts.path)) and _SUBSTACK in page.body:
        return urlunsplit(parts._replace(path=view.group(1)))
    return None


def fetch(url: str, client: httpx.Client) -> Page:
    """GET a URL. Raises FetchError with a short, human reason when that doesn't work out."""
    seen = {url}
    for _ in range(MAX_HOPS):
        page = _get(url, client)
        onward = _forwarding_address(page)
        if onward is None or onward in seen:
            return page
        seen.add(onward)
        url = onward
    raise FetchError("the link redirects in circles")


def _get(url: str, client: httpx.Client) -> Page:
    try:
        with client.stream("GET", url) as response:
            if response.status_code >= 400:
                reason = response.reason_phrase or "error"
                raise FetchError(f"the site answered {response.status_code} {reason}")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise FetchError(f"it's bigger than {MAX_BYTES // (1024 * 1024)} MB")
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            return Page(str(response.url), content_type, bytes(body), response.charset_encoding)
    except httpx.TimeoutException as e:
        raise FetchError("the site took too long to answer") from e
    except httpx.TooManyRedirects as e:
        raise FetchError("the link redirects in circles") from e
    except httpx.ConnectError as e:
        raise FetchError("couldn't connect (are you offline?)") from e
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise FetchError(str(e) or type(e).__name__) from e
