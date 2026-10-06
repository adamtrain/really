"""Find links in text and tidy them up, so the same page always has the same address."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from urllib.parse import unquote, urlsplit, urlunsplit

_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_BARE = re.compile(r"(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#]\S*)?", re.IGNORECASE)
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_SIGN_IN = re.compile(
    r"(^|[/.])(log-?in|sign-?in|sign_in|authwall|servicelogin|sso|oauth2?)([/.]|$)", re.IGNORECASE
)

# Query parameters that aim a link at a spot on a page, as a fragment does. Forums such as
# LessWrong link to a single comment this way, and serve that view with the site's own title.
POSITION_PARAMS = frozenset({"commentid"})

# Query parameters that say how you reached a page, not which page it is.
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_")
TRACKING_PARAMS = frozenset(
    {
        "__hsfp",
        "__hssc",
        "__hstc",
        "_bhlid",
        "_ga",
        "_gl",
        "_hsenc",
        "_hsmi",
        "ck_subscriber_id",
        "dclid",
        "fbclid",
        "gbraid",
        "gclid",
        "gclsrc",
        "igsh",
        "igshid",
        "li_fat_id",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "msclkid",
        "oly_anon_id",
        "oly_enc_id",
        "ref_src",
        "ref_url",
        "s_cid",
        "ttclid",
        "twclid",
        "vero_conv",
        "vero_id",
        "wbraid",
        "yclid",
    }
)


def _trim(url: str) -> str:
    """Drop the punctuation that trails a link in prose: "(see https://example.com/a)." """
    while url:
        last = url[-1]
        if last in ".,;:!?*_~" or (
            last in _CLOSERS and url.count(last) > url.count(_CLOSERS[last])
        ):
            url = url[:-1]
        else:
            break
    return url


def _has_host(url: str) -> bool:
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def find_urls(text: str) -> list[str]:
    """Every link in some text, in the order they appear, without repeats.

    A lone bare address like `example.com/post` counts too, and gets `https://`.
    """
    stripped = text.strip()
    if _BARE.fullmatch(stripped):
        return [f"https://{stripped}"]
    found: dict[str, None] = {}
    for match in _URL.finditer(text):
        url = _trim(match.group())
        if _has_host(url):
            found.setdefault(url)
    return list(found)


def _name(pair: str) -> str:
    return unquote(pair.split("=", 1)[0]).lower()


def _is_tracking(pair: str) -> bool:
    return _name(pair) in TRACKING_PARAMS or _name(pair).startswith(TRACKING_PREFIXES)


def _without(url: str, unwanted: Callable[[str], bool]) -> str:
    """A URL minus its fragment and the query parameters that `unwanted` picks out.

    What's left of the query string is kept byte for byte, since re-encoding it can break
    signed links.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = "&".join(pair for pair in parts.query.split("&") if pair and not unwanted(pair))
    # A fragment is a position on the page, unless the site uses it for routing (#/inbox, #!/a).
    fragment = parts.fragment if parts.fragment.startswith(("/", "!")) else ""
    return urlunsplit(parts._replace(query=query, fragment=fragment))


def page_of(url: str) -> str:
    """A link without the parts that only aim it at a spot on the page: the address to fetch.

    Tracking parameters stay, because a newsletter's redirector may need them to work.
    """
    return _without(url, lambda pair: _name(pair) in POSITION_PARAMS)


def bare(url: str) -> str:
    """A link with nothing after its path: no query string, no anchor."""
    return _without(url, lambda pair: True)


def clean(url: str) -> str:
    """Normalize a URL and strip what doesn't say which page it is: the address to know it by."""
    url = _without(url, lambda pair: _is_tracking(pair) or _name(pair) in POSITION_PARAMS)
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return url
    if not host:
        return url
    scheme = parts.scheme.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port and (scheme, port) not in {("http", 80), ("https", 443)}:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, parts.fragment))


def variants(url: str) -> list[str]:
    """The other addresses a page almost always answers to: http or https, with or without
    `www.`, with or without a slash on the end. For telling that you have something already
    when the page doesn't say which of them it prefers.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return []
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return []
    name = parts.netloc
    hosts = {name, name.removeprefix("www.") if name.startswith("www.") else f"www.{name}"}
    paths = {parts.path}
    if parts.path != "/":
        paths.add(parts.path.removesuffix("/") if parts.path.endswith("/") else f"{parts.path}/")
    found = {
        urlunsplit(parts._replace(scheme=scheme, netloc=netloc, path=path))
        for scheme in ("http", "https")
        for netloc in hosts
        for path in paths
    }
    return sorted(found - {url})


def host(url: str) -> str:
    """The site a URL belongs to, without the `www.`."""
    try:
        name = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return name.removeprefix("www.")


def is_sign_in(url: str) -> bool:
    """Whether a URL looks like a login page rather than something to read."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(_SIGN_IN.search(f"{parts.hostname or ''}{parts.path}"))


def slug(text: str, length: int = 60) -> str:
    """Some text as part of a file name: lowercase ASCII words joined by hyphens."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return words[:length].rstrip("-") or "untitled"


def title_from_url(url: str) -> str:
    """A stand-in title for a page we know nothing about: its slug, or failing that its host."""
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return url
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return host(url) or url
    slug = re.sub(r"\.(html?|php|aspx?|pdf|md|txt)$", "", segments[-1], flags=re.IGNORECASE)
    words = re.sub(r"[-_+]+", " ", slug).strip()
    if not re.search(r"[a-zA-Z]{3}", words):
        return f"{host(url)}{path}"
    return words[0].upper() + words[1:]
