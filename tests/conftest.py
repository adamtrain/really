from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from really.fetch import Page, new_client
from really.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

POST = "https://www.marginnotes.example/p/the-slow-web-is-still-here"
PAYWALLED = "https://www.marginnotes.example/p/what-the-archive-is-for"
BLOG = "https://fieldnotes.example/posts/tidy-queues"

# A forum that streams its pages the way LessWrong and the Alignment Forum do.
FORUM = "https://www.longtable.example/posts/k3Qw9xTz/on-keeping-a-commonplace-book"

# The same post, as linked from a reading sequence.
FORUM_IN_SEQUENCE = "https://www.longtable.example/s/n945eovrA3/p/k3Qw9xTz"

# A link to a PDF: a paper with no title in its metadata, twelve pages long.
PAPER = "https://papers.example/pdf/2609.01234"
PAPER_PDF_TEXT = "We find that reading lists grow without bound. "

# A page whose query string is its address, and a link whose query string is only decoration.
VIDEO = "https://videos.example/watch?v=abc123"
DECORATED = f"{BLOG}?ref=newsletter&share=1"

# Long enough to be worth timing.
LONGREAD = "https://longform.example/essays/the-long-one"

# The chain a link in a Substack email goes through before it reaches the post.
EMAILED = "https://substack.example/app-link/post?publication_id=1&post_id=2&token=secret"
SHARED = "https://open.substack.example/pub/marginnotes/p/the-slow-web-is-still-here?r=abc"
LANDED = f"{POST}?r=abc&utm_campaign=post&triedRedirect=true"
# What the "comment" button in a Substack email opens: every comment, and none of the post.
COMMENTS = f"{POST}/comments"
COMMENTS_VIEW = (
    f"<html><head><title>Comments - The Slow Web Is Still Here</title>"
    f'<link rel="canonical" href="{COMMENTS}">'
    '<link rel="stylesheet" href="https://substackcdn.com/bundle/theme/main.css"></head>'
    '<body><div class="comments-page"><div class="comment"><div class="comment-body">'
    "<p>Respectfully, the queue is the problem.</p></div></div></div></body></html>"
)
INTERSTITIAL = (
    f'<head><noscript><META http-equiv="refresh" content="0;URL={LANDED.replace("&", "&#38;")}">'
    f'</noscript></head><script>location.replace("{LANDED}")</script>'
)


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def page(name: str, url: str) -> Page:
    return Page(url, "text/html", load(name), "utf-8")


def html(title: str, body: str, head: str = "") -> bytes:
    """A small page with enough prose in it to count as an article."""
    filler = "<p>" + " ".join(["Plain words that fill out a paragraph nicely."] * 12) + "</p>"
    document = (
        f"<html><head><title>{title}</title>{head}</head>"
        f"<body><article><h1>{title}</h1>{body}{filler}</article></body></html>"
    )
    return document.encode()


def essay(title: str, paragraphs: int = 40, head: str = "") -> bytes:
    """A page with a couple of thousand words on it, no two paragraphs the same."""
    body = "".join(
        f"<p>Thought {n} concerns reading, and queues, and the time that both of them take. "
        f"It follows from thought {n - 1}, more or less, and it leads on to the next one. "
        "Nobody has ever finished a reading list, which is no reason not to keep one.</p>"
        for n in range(1, paragraphs + 1)
    )
    return html(title, body, head=head)


def pdf(
    text: str, title: str = "", author: str = "", heading: str = "", stamp: str = "", pages: int = 1
) -> bytes:
    """A minimal PDF, written by hand.

    `heading` is set in large type above the text, as a paper's title is. `stamp` is set
    larger still but sideways in the margin, as arXiv marks its papers. Each of `pages`
    pages carries the same content.
    """
    drawn = [f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET"]
    if heading:
        drawn.insert(0, f"BT /F1 24 Tf 72 740 Td ({heading}) Tj ET")
    if stamp:
        drawn.insert(0, f"BT /F1 30 Tf 0 1 -1 0 40 300 Tm ({stamp}) Tj ET")
    stream = "\n".join(drawn).encode()
    info = f"<< /Title ({title}) /Author ({author}) >>".encode()
    leaf = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>"
    )
    kids = " ".join(f"{7 + n} 0 R" for n in range(pages - 1))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [3 0 R {kids}] /Count {pages} >>".encode(),
        leaf,
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        info,
        *[leaf] * (pages - 1),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


class Web:
    """A pretend internet: canned responses by URL, served through httpx's mock transport."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[], httpx.Response]] = {}
        self.requests: list[str] = []

    def serve(self, url: str, body: bytes | str, content_type: str = "text/html; charset=utf-8"):
        content = body.encode() if isinstance(body, str) else body
        headers = {"content-type": content_type}
        self.routes[url] = lambda: httpx.Response(200, content=content, headers=headers)

    def redirect(self, url: str, to: str) -> None:
        self.routes[url] = lambda: httpx.Response(302, headers={"location": to})

    def status(self, url: str, code: int) -> None:
        self.routes[url] = lambda: httpx.Response(code)

    def fail(self, url: str, error: Exception) -> None:
        def explode() -> httpx.Response:
            raise error

        self.routes[url] = explode

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        route = self.routes.get(str(request.url))
        return route() if route else httpx.Response(404)

    def client(self) -> httpx.Client:
        return new_client(httpx.MockTransport(self.handle))


@pytest.fixture
def web() -> Web:
    web = Web()
    web.serve(POST, load("substack_post.html"))
    web.serve(LANDED, load("substack_post.html"))
    web.serve(PAYWALLED, load("substack_paywalled.html"))
    web.serve(BLOG, load("blog_post.html"))
    web.serve(FORUM, load("forum_post.html"))
    web.serve(FORUM_IN_SEQUENCE, load("forum_post.html"))
    web.redirect(EMAILED, SHARED)
    web.serve(COMMENTS, COMMENTS_VIEW)
    web.serve(VIDEO, essay("A Talk Worth Watching"))
    web.serve("https://videos.example/watch", html("Videos", "<p>Pick something to watch.</p>"))
    web.serve(DECORATED, load("blog_post.html"))
    paper = pdf(PAPER_PDF_TEXT * 12, heading="Reading Lists Grow Without Bound", pages=12)
    web.serve(PAPER, paper, content_type="application/pdf")
    web.serve(LONGREAD, essay("The Long One"))
    for n in range(1, 8):
        web.serve(f"{LONGREAD}?part={n}", essay(f"The Long One, Part {n}"))
    web.serve(SHARED, INTERSTITIAL)
    return web


class Clock:
    """Stands in for the clock, so a test can let time pass."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **duration: float) -> None:
        self.now += timedelta(**duration)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("really.store._now", clock)
    monkeypatch.setattr("really.render._now", clock)
    return clock


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "really.db") as store:
        yield store
