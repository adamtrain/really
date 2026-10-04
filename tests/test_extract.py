import subprocess
import sys

from really.extract import extract
from really.fetch import Page

from .conftest import BLOG, LANDED, PAYWALLED, POST, html, page, pdf


def test_substack_post():
    article = extract(page("substack_post.html", POST))
    assert article.title == "The Slow Web Is Still Here"
    assert article.author == "Ada Quill"
    assert article.site == "Margin Notes"
    assert article.published == "2026-03-14"
    assert article.summary == "Notes on reading things properly."
    assert not article.paywalled
    assert 200 < article.words < 300


def test_only_the_article_is_kept():
    article = extract(page("substack_post.html", POST))
    assert "lighthouse" in article.text
    assert "Keep it or let it go." in article.text
    for chrome in ("Type your email", "reader-supported", "Privacy", "Write a comment"):
        assert chrome not in article.text
        assert chrome not in article.content


def test_comments_are_not_part_of_the_article():
    article = extract(page("substack_post.html", POST))
    for remark in (
        "subscribed immediately",
        "the queue is the problem",
        "Bartholomew Finch",
        "Discussion about this post",
        "See all 41 comments",
    ):
        assert remark not in article.text
        assert remark not in article.content


def test_footnotes_are_kept_but_do_not_count_toward_reading_time():
    article = extract(page("substack_post.html", POST))
    assert "palimpsest" in article.text
    assert "palimpsest" in article.content
    assert article.footnote_words > 40
    assert article.words == len(article.text.split()) - article.footnote_words
    assert 235 < article.words < 250  # the article proper is 242 words


def test_footnotes_in_other_markup():
    notes = (
        '<section class="footnotes" role="doc-endnotes"><ol><li id="fn1"><p>'
        + "An aside that nobody should have to time. " * 20
        + "</p></li></ol></section>"
    )
    url = "https://example.com/annotated"
    annotated = extract(Page(url, "text/html", html("Annotated", notes)))
    plain = extract(Page(url, "text/html", html("Annotated", "")))
    assert "aside that nobody" in annotated.text
    assert annotated.footnote_words > 100
    assert annotated.words == plain.words
    assert plain.footnote_words == 0


def test_content_is_markdown_and_text_is_plain():
    article = extract(page("substack_post.html", POST))
    assert "[a dozen inboxes](https://example.org/inboxes)" in article.content
    assert "## Keeping what you finish" in article.content
    assert "https://example.org" not in article.text
    assert "#" not in article.text


def test_the_title_and_subtitle_are_not_repeated_at_the_top():
    article = extract(page("substack_post.html", POST))
    assert article.content.startswith("Every few months")
    assert article.text.startswith("Every few months")
    assert article.summary == "Notes on reading things properly."


def test_canonical_drops_what_a_newsletter_link_picks_up():
    article = extract(page("substack_post.html", LANDED))
    assert article.canonical == POST


def test_a_canonical_pointing_elsewhere_is_ignored():
    """The blog fixture declares its homepage as canonical, as misconfigured sites do."""
    assert extract(page("blog_post.html", BLOG)).canonical == ""


def test_paywalled_post_is_flagged_and_keeps_its_preview():
    article = extract(page("substack_paywalled.html", PAYWALLED))
    assert article.paywalled
    assert article.title == "What the Archive Is For"
    assert "marginalia" in article.text
    assert "lighthouse" not in article.text
    assert "paid subscribers" not in article.text
    assert "Already a paid subscriber" not in article.text


def test_blog_post():
    article = extract(page("blog_post.html", BLOG))
    assert article.title == "Tidy Queues, Tidy Mind"  # without " | Fieldnotes"
    assert article.author == "Sam Okafor"
    assert article.site == "Fieldnotes"
    assert article.published == "2025-11-02"
    assert article.summary.startswith("A queue is a promise")
    assert article.summary.endswith("…")


def test_a_twitter_handle_is_not_a_site_name():
    body = html("Fast Tools", "", head='<meta name="twitter:site" content="@fast_tools">')
    article = extract(Page("https://www.fast.example/blog/tools", "text/html", body))
    assert article.site == "fast.example"


def test_paper_authors_are_put_the_right_way_round():
    head = "".join(
        f'<meta name="citation_author" content="{name}">'
        for name in ("Vaswani, Ashish", "Shazeer, Noam", "Gomez, Aidan N.")
    )
    article = extract(Page("https://papers.example/abs/1", "text/html", html("A Paper", "", head)))
    assert article.author == "Ashish Vaswani, Noam Shazeer, Aidan N. Gomez"


def test_wikipedia_has_no_byline():
    body = html("Zettelkasten - Wikipedia", '<div class="author">Authority control</div>')
    article = extract(Page("https://en.wikipedia.org/wiki/Zettelkasten", "text/html", body))
    assert article.author == ""
    assert article.title == "Zettelkasten"


def test_dates_are_not_guessed_from_the_text():
    body = html("Undated", "<p>Back on 12 March 2019 something happened.</p>")
    assert extract(Page("https://example.com/undated", "text/html", body)).published == ""


def test_a_page_with_no_article_still_has_a_title():
    body = b"<html><head><title>Sign in</title></head><body><form></form></body></html>"
    article = extract(Page("https://example.com/login", "text/html", body))
    assert article.title == "Sign in"
    assert article.words == 0


def test_declared_charset_wins():
    body = "<html><head><title>Café</title></head><body><p>x</p></body></html>".encode("latin-1")
    assert extract(Page("https://example.com/c", "text/html", body, "iso-8859-1")).title == "Café"


def test_plain_text():
    article = extract(Page("https://example.com/notes.txt", "text/plain", b"Just some notes.\n"))
    assert article.text == article.content == "Just some notes."
    assert article.title == ""


def test_pdf():
    body = pdf("Search engines index words, not pages.", title="On Indexing", author="R. Reader")
    article = extract(Page("https://example.com/paper.pdf", "application/pdf", body))
    assert article.title == "On Indexing"
    assert article.author == "R. Reader"
    assert "index words" in article.text
    assert article.published == ""


def test_pdf_served_with_the_wrong_content_type():
    article = extract(Page("https://example.com/download", "application/octet-stream", pdf("Hi")))
    assert article.text == "Hi"


def test_pdf_title_that_is_really_a_filename():
    body = pdf("Text", title="Microsoft Word - draft_final.docx")
    assert extract(Page("https://example.com/a.pdf", "application/pdf", body)).title == ""


def test_unreadable_things_are_empty_not_errors():
    assert (
        extract(Page("https://example.com/a.pdf", "application/pdf", b"%PDF-1.4 nope")).words == 0
    )
    assert extract(Page("https://example.com/a.png", "image/png", b"\x89PNG")).words == 0
    assert extract(Page("https://example.com/", "text/html", b"")).words == 0


def test_extracting_is_quiet_on_free_threaded_python():
    """There, loading lxml switches the GIL back on and Python warns about it on stderr.

    It only happens on a module's first import, so this needs an interpreter of its own.
    """
    code = (
        "from really.extract import extract\n"
        "from really.fetch import Page\n"
        "extract(Page('https://example.com/', 'text/html', b'<p>hello</p>'))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
