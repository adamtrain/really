import httpx
import pytest

from really.fetch import INTERSTITIAL_BYTES, FetchError, fetch

from .conftest import COMMENTS, EMAILED, LANDED, POST, SHARED


def test_fetches_a_page(web):
    with web.client() as client:
        page = fetch(POST, client)
    assert page.url == POST
    assert page.content_type == "text/html"
    assert page.charset == "utf-8"
    assert b"lighthouse" in page.body


def test_follows_an_emailed_link_through_its_redirects(web):
    """An HTTP redirect, then a page that forwards with <meta refresh>, like Substack's."""
    with web.client() as client:
        page = fetch(EMAILED, client)
    assert page.url == LANDED
    assert web.requests == [EMAILED, SHARED, LANDED]


def test_a_substack_comments_view_leads_to_the_post(web):
    with web.client() as client:
        page = fetch(COMMENTS, client)
    assert page.url == POST
    assert b"lighthouse" in page.body


def test_a_comments_page_anywhere_else_is_left_alone(web):
    url = "https://forum.example/p/a-thread/comments"
    web.serve(url, "<p>Everyone's comments.</p>")
    with web.client() as client:
        assert fetch(url, client).url == url


def test_meta_refresh_loops_end(web):
    a, b = "https://a.example/", "https://b.example/"
    web.serve(a, f'<meta http-equiv="refresh" content="0; url={b}">')
    web.serve(b, f"<meta http-equiv='refresh' content='0;URL={a}'>")
    with web.client() as client:
        assert fetch(a, client).url == b


def test_a_real_page_that_refreshes_itself_is_not_an_interstitial(web):
    url = "https://live.example/scores"
    padding = "<p>score</p>" * INTERSTITIAL_BYTES
    web.serve(url, f'<meta http-equiv="refresh" content="30;url=https://live.example/x">{padding}')
    with web.client() as client:
        assert fetch(url, client).url == url


def test_relative_refresh_targets_resolve(web):
    web.serve("https://a.example/go", '<meta http-equiv="refresh" content="0;url=/landed">')
    web.serve("https://a.example/landed", "<p>here</p>")
    with web.client() as client:
        assert fetch("https://a.example/go", client).url == "https://a.example/landed"


def test_missing_page(web):
    with web.client() as client, pytest.raises(FetchError, match="404"):
        fetch("https://nowhere.example/", client)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (httpx.ReadTimeout("slow"), "too long"),
        (httpx.ConnectError("no route"), "offline"),
        (httpx.TooManyRedirects("round and round"), "circles"),
    ],
)
def test_network_trouble_is_explained(web, error, message):
    web.fail("https://flaky.example/", error)
    with web.client() as client, pytest.raises(FetchError, match=message):
        fetch("https://flaky.example/", client)


def test_undeclared_charset_is_left_for_the_extractor(web):
    web.serve("https://old.example/", b"<p>caf\xe9</p>", content_type="text/html")
    with web.client() as client:
        assert fetch("https://old.example/", client).charset is None
