"""Work out what a fetched page is: its title, who wrote it, and the article itself.

The heavy lifting is trafilatura's, which strips a page down to its main text. What's here
decides which of its guesses to trust, and covers PDFs and plain text.
"""

from __future__ import annotations

import io
import logging
import re
import warnings
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import urls
from .fetch import Page

SUMMARY_LENGTH = 280
OPENING = 200  # how much of two articles' text has to match for them to be the same one

# With these, a publication date only comes from the page's own metadata, never from a guess
# based on whatever dates happen to appear in the text.
DATES = {"extensive_search": False, "original_date": True}

# Footnotes belong to the article, so they're saved and searched. But they're a detour from
# it, so they don't count toward how long it takes to read (Substack's own counts agree).
FOOTNOTES = (
    "//*[self::div or self::section or self::aside or self::ol or self::ul]["
    'contains(concat(" ", normalize-space(@class), " "), " footnote ")'
    ' or contains(concat(" ", normalize-space(@class), " "), " footnotes ")'
    ' or @role="doc-endnotes"]'
)

# How a page says "this part is the article": the read-later tools' convention, and schema.org's.
DECLARED_BODY = (
    '//*[contains(concat(" ", normalize-space(@class), " "), " instapaper_body ")'
    ' or @itemprop="articleBody"]'
)
# A part of the page that's named for comments.
_COMMENTS = (
    "not(self::html or self::body) and contains("
    'translate(concat(@class, " ", @id), "COMENT", "coment"), "comment")'
)

# Sites written by everyone and so by no one. Their "author" is always a false positive.
NO_BYLINE = ("wikipedia.org",)

_PAYWALLED = re.compile(rb'"isAccessibleForFree"\s*:\s*"?false', re.IGNORECASE)
_SEPARATORS = "-|\u2013\u2014\u00b7:\u00bb"  # hyphen, bar, dashes, middle dot, colon, »
_TITLE_SEPARATOR = re.compile(rf"\s+[{_SEPARATORS}]+\s+")
_FILE_LIKE = re.compile(r"^(microsoft word - |untitled)|\.(docx?|pdf|tex|dvi|indd)$", re.IGNORECASE)

for _noisy in ("trafilatura", "htmldate", "courlan", "justext", "pypdf"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

# On free-threaded Python, loading lxml switches the GIL back on, and Python says so on stderr.
# That's the safe outcome, and nothing someone adding a link can do anything about.
warnings.filterwarnings("ignore", "The global interpreter lock", RuntimeWarning)


@dataclass(frozen=True, slots=True)
class Article:
    title: str = ""
    author: str = ""
    site: str = ""
    published: str = ""  # an ISO date, when the page states one
    summary: str = ""
    content: str = ""  # Markdown, links intact: the copy you read
    text: str = ""  # plain text: the copy that gets searched
    canonical: str = ""  # the page's preferred address for itself, when that can be trusted
    paywalled: bool = False  # the page says the full text is for subscribers
    footnote_words: int = 0  # how much of the text is footnotes

    @property
    def words(self) -> int:
        """The length of the article proper: what reading time is based on."""
        return max(0, len(self.text.split()) - self.footnote_words)


def same_article(one: Article, other: Article) -> bool:
    """Whether two fetches came back with the same article.

    The same title, the same opening and about the same length: a view counter may have
    ticked over in between, but a different page, a preview or an error page won't pass.
    """
    if not one.text or one.title != other.title:
        return False
    close = abs(one.words - other.words) <= max(one.words, other.words) * 0.02
    return close and one.text[:OPENING] == other.text[:OPENING]


def extract(page: Page) -> Article:
    """Turn a downloaded page into an Article. Never raises: an unreadable page is an empty one."""
    kind = page.content_type
    if kind == "application/pdf" or page.body.startswith(b"%PDF-"):
        return _from_pdf(page)
    if kind.startswith("text/") and "html" not in kind and "xml" not in kind:
        text = _decode(page).strip()
        return Article(summary=_excerpt(text), content=text, text=text)
    if "html" in kind or "xml" in kind or not kind:
        return _from_html(page)
    return Article()


def _decode(page: Page) -> str:
    return page.body.decode(page.charset or "utf-8", errors="replace")


def _squash(value: str | None) -> str:
    return " ".join((value or "").split())


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _excerpt(text: str, length: int = SUMMARY_LENGTH) -> str:
    text = _squash(text).lstrip("|#> ")
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


# ── HTML ──────────────────────────────────────────────────────────────────────


def _from_html(page: Page) -> Article:
    from lxml.html import tostring
    from trafilatura import bare_extraction, extract_metadata, load_html
    from trafilatura import extract as extract_text
    from trafilatura.settings import Document

    # Let the server's declared charset win; otherwise trafilatura sniffs the bytes itself.
    source = _decode(page) if page.charset else page.body
    tree = load_html(source)
    if tree is None:
        return Article()
    if _repair(tree):
        source = tostring(tree, encoding="unicode")

    doc = bare_extraction(
        source,
        url=page.url,
        with_metadata=True,
        include_comments=False,
        include_links=True,
        include_formatting=True,
        date_extraction_params=DATES,
    )
    footnote_words = 0
    if isinstance(doc, Document):
        content = (doc.text or "").strip()
        text = (extract_text(source, url=page.url, include_comments=False) or "").strip()
        if tree.xpath(FOOTNOTES):
            body = extract_text(source, url=page.url, include_comments=False, prune_xpath=FOOTNOTES)
            footnote_words = max(0, len(text.split()) - len((body or "").split()))
    else:  # too little text to count as an article, but the metadata is still worth having
        doc = extract_metadata(tree, default_url=page.url, date_config=DATES)
        content = text = ""

    host = urls.host(page.url)
    site = _site(tree, doc.sitename, host)
    title, named = _tidy_title(_squash(doc.title), site, host)
    if named and site == host:  # the title's "— LessWrong" is the only place the site is named
        site = named
    description = _squash(doc.description)
    # Shown as the title and summary already, so not wanted again as the copy's first lines.
    for repeated in (title, description):
        content, text = _without_heading(content, repeated), _without_heading(text, repeated)
    return Article(
        title=title,
        author=_authors(tree, doc.author, host),
        site=site,
        published=_published(tree, page.url),
        summary=description if re.search(r"\w", description) else _excerpt(text),
        content=content,
        text=text,
        canonical=_canonical(tree, page.url),
        paywalled=bool(_PAYWALLED.search(page.body)),
        footnote_words=footnote_words,
    )


def _repair(tree) -> bool:
    """Undo two things pages do that mislead the extractor. True if there was anything to undo.

    A page streamed in pieces, as React apps like LessWrong's are, leaves its <title> and <meta>
    tags in the body. A browser moves them up into the head; the extractor only looks there.

    And the extractor throws away anything named for comments, which is nearly always right.
    But where a page marks its article, the mark outranks a name: LessWrong wraps each post in
    "commentOnSelection", for commenting on a highlight, and the whole post went with it.
    """
    changed = False
    head = tree.find("head")
    if head is not None:
        misplaced = tree.xpath("//body//meta[@name or @property] | //body//link[@rel='canonical']")
        if head.find("title") is None:
            misplaced += tree.xpath("//body//title[not(ancestor::svg)]")[:1]
        for tag in misplaced:
            _detach(tag)
            head.append(tag)
            changed = True
    for body in tree.xpath(DECLARED_BODY):
        for element in (*body.iter("*"), *body.iterancestors()):
            for attribute in ("class", "id"):
                if "comment" in (element.get(attribute) or "").lower():
                    del element.attrib[attribute]
                    changed = True
    return changed


def _detach(element) -> None:
    """Take an element out of the tree, leaving behind the text that follows it."""
    parent, before = element.getparent(), element.getprevious()
    if element.tail and before is not None:
        before.tail = (before.tail or "") + element.tail
    elif element.tail:
        parent.text = (parent.text or "") + element.tail
    element.tail = None
    parent.remove(element)


def _published(tree, url: str) -> str:
    """The publication date, as far as the page states one.

    The date library reads a page's metadata, its address and its dateline well. But with none
    of those to go on it keeps looking, and comes back with a timestamp out of a script
    (LessWrong's settings hold several) or the date on a reader's comment. So it's shown the
    page without its scripts, other than structured data, and without its comments.
    """
    from htmldate import find_date

    page = deepcopy(tree)
    for stray in page.xpath(f"//script[not(@type='application/ld+json')] | //*[{_COMMENTS}]"):
        if stray.getparent() is not None:
            _detach(stray)
    found = find_date(
        page,
        url=url,
        extensive_search=DATES["extensive_search"],
        original_date=DATES["original_date"],
    )
    return found or ""


def _without_heading(body: str, heading: str) -> str:
    """Drop an opening line that only repeats the title or the subtitle."""
    first, _, rest = body.partition("\n")
    if heading and _key(first) == _key(heading):
        return rest.lstrip()
    return body


def _meta(tree, attribute: str, name: str) -> list[str]:
    return [_squash(value) for value in tree.xpath(f'//meta[@{attribute}="{name}"]/@content')]


def _site(tree, guess: str | None, host: str) -> str:
    """The publication's name, falling back to its hostname."""
    name = _squash(guess)
    handles = {_key(handle) for handle in _meta(tree, "name", "twitter:site")}
    # trafilatura will settle for a Twitter handle ("Astral_Sh"), which is worse than the host.
    if not name or _key(name) in handles:
        return host
    return name


def _tidy_title(title: str, site: str, host: str) -> tuple[str, str]:
    """Split a trailing " - Site Name" off a title. Returns the title, and that name if any."""
    pieces = _TITLE_SEPARATOR.split(title)
    if len(pieces) < 2:
        return title, ""
    last = _key(pieces[-1])
    names = {_key(site), _key(host), *(_key(label) for label in host.split(".")[:-1])} - {""}
    # The name and the address needn't match exactly: "AI Alignment Forum", alignmentforum.org.
    alike = len(last) > 3 and any(len(n) > 3 and (n in last or last in n) for n in names)
    if last in names or alike:
        return title[: title.rindex(pieces[-1])].rstrip(" " + _SEPARATORS), pieces[-1].strip()
    return title, ""


def _authors(tree, guess: str | None, host: str) -> str:
    if host.endswith(NO_BYLINE):
        return ""
    # Papers list their authors one per tag, as "Last, First".
    cited = _meta(tree, "name", "citation_author")
    if cited:
        names = [" ".join(reversed(name.split(", ", 1))) for name in cited]
    else:
        names = [_squash(name) for name in (guess or "").split(";")]
    return ", ".join(dict.fromkeys(name for name in names if name))


def _canonical(tree, fetched: str) -> str:
    """The page's <link rel=canonical>, when it's believably another address for this page.

    Canonical links are how the junk on a newsletter link (?publication_id=…&r=…) gets
    dropped, and how one post reached three ways turns out to be one post. But sites also get
    them wrong, pointing every page at the home page, say. So one is believed only if it's on
    the same site and either has the same path, or still has in it the last piece of the path
    we fetched: /posts/HBxe6wdj and /s/sequence/p/HBxe6wdj are both, canonically,
    /posts/HBxe6wdj/what-failure-looks-like.
    """
    for href in tree.xpath('//link[@rel="canonical"]/@href'):
        try:
            ours, theirs = urlsplit(fetched), urlsplit(href.strip())
        except ValueError:
            continue
        if (theirs.scheme, theirs.hostname) != (ours.scheme, ours.hostname):
            continue
        last = ours.path.rstrip("/").rpartition("/")[2]
        if theirs.path == ours.path or (len(last) >= 8 and last in theirs.path.split("/")):
            return href.strip()
    return ""


# ── PDF ───────────────────────────────────────────────────────────────────────


def _from_pdf(page: Page) -> Article:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(page.body))
        pages = [(leaf.extract_text() or "").strip() for leaf in reader.pages]
        info = reader.metadata
        title = _squash(info.title if info else None)
        author = _squash(info.author if info else None)
    except Exception:  # pypdf raises all sorts on malformed files
        return Article()
    text = "\n\n".join(leaf for leaf in pages if leaf)
    return Article(
        title="" if _FILE_LIKE.search(title) else title,
        author=author,
        summary=_excerpt(text),
        content=text,
        text=text,
    )
