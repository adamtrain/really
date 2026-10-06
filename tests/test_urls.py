import pytest

from really import urls


def test_finds_links_in_prose_without_their_punctuation():
    text = "Read https://example.com/a, then (https://example.com/b). Also <https://example.com/c>!"
    assert urls.find_urls(text) == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]


def test_keeps_parentheses_that_belong_to_the_link():
    link = "https://en.wikipedia.org/wiki/Mercury_(planet)"
    assert urls.find_urls(f"See {link}.") == [link]
    assert urls.find_urls(f"[Mercury]({link})") == [link]


def test_repeats_are_dropped_and_order_is_kept():
    text = "https://b.example/1 https://a.example/2 https://b.example/1"
    assert urls.find_urls(text) == ["https://b.example/1", "https://a.example/2"]


def test_a_lone_bare_address_counts():
    assert urls.find_urls(" example.com/post?x=1 ") == ["https://example.com/post?x=1"]


@pytest.mark.parametrize("text", ["", "hello there", "list", "see example.com sometime", "http://"])
def test_text_without_links(text):
    assert urls.find_urls(text) == []


def test_clean_strips_tracking_but_keeps_real_parameters():
    dirty = (
        "https://Example.com/post?utm_source=newsletter&id=42&fbclid=abc&utm_medium=email&page=2"
    )
    assert urls.clean(dirty) == "https://example.com/post?id=42&page=2"


def test_clean_leaves_the_remaining_query_byte_for_byte():
    signed = "https://example.com/file?sig=a%2Bb/c==&utm_campaign=x&name=caf%C3%A9+au+lait"
    assert urls.clean(signed) == "https://example.com/file?sig=a%2Bb/c==&name=caf%C3%A9+au+lait"


@pytest.mark.parametrize(
    ("given", "cleaned"),
    [
        ("https://example.com", "https://example.com/"),
        ("https://example.com/a#section-2", "https://example.com/a"),
        ("https://example.com/#/inbox/3", "https://example.com/#/inbox/3"),
        ("HTTPS://EXAMPLE.com:443/a", "https://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com/a?utm_source=x", "https://example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_clean(given, cleaned):
    assert urls.clean(given) == cleaned


def test_a_link_to_one_comment_is_a_link_to_its_page():
    link = "https://forum.example/posts/abc/a-post?commentId=x9Yz&page=2&utm_source=share#top"
    assert urls.clean(link) == "https://forum.example/posts/abc/a-post?page=2"
    # For fetching, only what points at a place on the page goes; a redirector may want the rest.
    assert urls.page_of(link) == "https://forum.example/posts/abc/a-post?page=2&utm_source=share"
    assert urls.page_of("https://example.com/a?b=1") == "https://example.com/a?b=1"


def test_bare_is_a_link_with_nothing_after_the_path():
    assert urls.bare("https://example.com/post?id=42&ref=x#notes") == "https://example.com/post"
    assert urls.bare("https://example.com/post") == "https://example.com/post"
    assert urls.bare("https://app.example/?tab=2#/inbox/3") == "https://app.example/#/inbox/3"


def test_clean_is_idempotent():
    once = urls.clean("https://example.com/a?b=1&utm_source=x#top")
    assert urls.clean(once) == once


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.google.com/ServiceLogin?continue=https://docs.google.com/x",
        "https://www.linkedin.com/authwall?trk=x",
        "https://x.com/i/flow/login",
        "https://substack.com/sign-in?redirect=%2Fp%2Fpost",
        "https://login.example.com/",
        "https://example.com/account/login.php",
    ],
)
def test_sign_in_pages(url):
    assert urls.is_sign_in(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/blog/login-flows-explained",
        "https://example.com/p/designing-a-sign-in-page",
        "https://blogin.example.com/post",
        "https://example.com/",
    ],
)
def test_articles_about_signing_in_are_not_sign_in_pages(url):
    assert not urls.is_sign_in(url)


def test_variants_are_the_other_addresses_a_page_answers_to():
    assert urls.variants("https://example.com/a?id=1") == [
        "http://example.com/a/?id=1",
        "http://example.com/a?id=1",
        "http://www.example.com/a/?id=1",
        "http://www.example.com/a?id=1",
        "https://example.com/a/?id=1",
        "https://www.example.com/a/?id=1",
        "https://www.example.com/a?id=1",
    ]
    assert "https://example.com/a" in urls.variants("http://www.example.com/a/")
    assert urls.variants("https://example.com/") == [
        "http://example.com/",
        "http://www.example.com/",
        "https://www.example.com/",
    ]
    assert urls.variants("not a link") == []


def test_host_drops_www():
    assert urls.host("https://www.Example.com/a") == "example.com"
    assert urls.host("not a url") == ""


@pytest.mark.parametrize(
    ("url", "title"),
    [
        ("https://example.com/blog/my-great_post.html", "My great post"),
        ("https://example.com/", "example.com"),
        ("https://arxiv.org/pdf/1706.03762", "arxiv.org/pdf/1706.03762"),
        ("https://example.com/caf%C3%A9-notes/", "Café notes"),
    ],
)
def test_title_from_url(url, title):
    assert urls.title_from_url(url) == title
