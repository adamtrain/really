import pytest

from really.expiry import covering, parse_span, rule_for, site_of

HOUR, DAY, WEEK = 3600, 86400, 604800


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("3d", 3 * DAY),
        ("3", 3 * DAY),  # a bare number is days
        ("3 days", 3 * DAY),
        ("1 day", DAY),
        ("12h", 12 * HOUR),
        ("36 hours", 36 * HOUR),
        ("2w", 2 * WEEK),
        ("2 Weeks", 2 * WEEK),
        ("1.5d", 36 * HOUR),
        (" 5d ", 5 * DAY),
    ],
)
def test_parse_span(text, seconds):
    assert parse_span(text) == seconds


@pytest.mark.parametrize("text", ["", "soon", "3 months", "d", "-2d", "0d", "3d 4h"])
def test_spans_that_make_no_sense(text):
    with pytest.raises(ValueError):
        parse_span(text)


@pytest.mark.parametrize(
    ("given", "site"),
    [
        ("thezvi.substack.com", "thezvi.substack.com"),
        ("www.TheZvi.Substack.com", "thezvi.substack.com"),
        ("https://thezvi.substack.com/p/ai-150?utm_source=x", "thezvi.substack.com"),
        ("zvi", ""),
        ("", ""),
    ],
)
def test_site_of(given, site):
    assert site_of(given) == site


def test_a_rule_covers_its_site_and_everything_under_it():
    rules = {"example.com": 7 * DAY, "news.example.com": DAY}
    assert rule_for("example.com", rules) == 7 * DAY
    assert rule_for("blog.example.com", rules) == 7 * DAY
    assert rule_for("news.example.com", rules) == DAY  # the nearer rule wins
    assert rule_for("live.news.example.com", rules) == DAY
    assert rule_for("notexample.com", rules) is None
    assert rule_for("example.org", rules) is None
    assert rule_for("example.com", {}) is None
    assert covering("live.news.example.com", rules) == "news.example.com"
    assert covering("blog.example.com", rules) == "example.com"
    assert covering("example.org", rules) is None
