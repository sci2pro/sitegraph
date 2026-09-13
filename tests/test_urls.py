"""Tests for canonical URL identity.

Every rule in `sitegraph.urls` has a case here, plus the `NAIVE_WRONG` table
below, which pins the cases a plausible-but-wrong implementation gets wrong.
Those are the ones that matter: a normalizer that is merely incomplete produces
duplicate nodes and looks like a crawler bug, not a URL bug.
"""

from __future__ import annotations

import pytest

from sitegraph.urls import (
    InvalidSkipPattern,
    InvalidURL,
    Origin,
    SkipRules,
    internal_links,
    is_internal,
    is_loopback,
    normalize_url,
    origin_of,
    resolve_href,
    url_shape,
)

# (input, expected canonical form)
NORMALIZE_CASES = [
    # --- fragments are discarded (spec §4) ---
    ("http://localhost:3000/about#team", "http://localhost:3000/about"),
    ("http://localhost:3000/about#history", "http://localhost:3000/about"),
    ("http://localhost:3000/about#", "http://localhost:3000/about"),
    # --- scheme and host fold, path and query do not ---
    ("HTTP://Example.COM/Path", "http://example.com/Path"),
    ("http://LOCALHOST:3000/About", "http://localhost:3000/About"),
    ("http://example.com/UPPER", "http://example.com/UPPER"),
    # --- empty path becomes "/" ---
    ("http://localhost:3000", "http://localhost:3000/"),
    ("http://localhost:3000?q=1", "http://localhost:3000/?q=1"),
    ("http://example.com", "http://example.com/"),
    # --- default ports dropped, every other port kept ---
    ("http://example.com:80/x", "http://example.com/x"),
    ("https://example.com:443/x", "https://example.com/x"),
    # 443 is *not* the default for http, and 80 is not for https
    ("http://example.com:443/x", "http://example.com:443/x"),
    ("https://example.com:80/x", "https://example.com:80/x"),
    ("http://localhost:3000/", "http://localhost:3000/"),
    # --- trailing slashes are distinct, so they are preserved ---
    ("http://example.com/about/", "http://example.com/about/"),
    ("http://example.com/about", "http://example.com/about"),
    # --- duplicate slashes are preserved ---
    ("http://example.com//a//b", "http://example.com//a//b"),
    # --- dot segments (RFC 3986 §5.2.4) ---
    ("http://example.com/a/./b", "http://example.com/a/b"),
    ("http://example.com/a/../b", "http://example.com/b"),
    ("http://example.com/a/b/..", "http://example.com/a/"),
    ("http://example.com/a/b/.", "http://example.com/a/b/"),
    ("http://example.com/../x", "http://example.com/x"),
    ("http://example.com/..", "http://example.com/"),
    ("http://example.com/a/b/c/../../d", "http://example.com/a/d"),
    # --- backslashes are separators to the browser ---
    ("http://example.com/a\\b", "http://example.com/a/b"),
    # --- query is significant, and its parameter order is not normalized ---
    ("http://example.com/s?b=2&a=1", "http://example.com/s?b=2&a=1"),
    ("http://example.com/s?a=1&b=2", "http://example.com/s?a=1&b=2"),
    ("http://example.com/s?a=1+2", "http://example.com/s?a=1+2"),
    # --- a bare trailing "?" carries no information ---
    ("http://example.com/x?", "http://example.com/x"),
    # --- percent-encoding: uppercase hex, decode unreserved only ---
    ("http://example.com/%c3%a9", "http://example.com/%C3%A9"),
    ("http://example.com/%7Euser", "http://example.com/~user"),
    ("http://example.com/s?q=%7E", "http://example.com/s?q=~"),
    # %2F must never become "/" — it is not a separator
    ("http://example.com/a%2Fb", "http://example.com/a%2Fb"),
    # %2E must never become "." — "/a/%2E%2E/b" is a different resource
    ("http://example.com/a/%2E%2E/b", "http://example.com/a/%2E%2E/b"),
    # a stray or truncated escape is encoded, never passed through (see the
    # idempotence test below for why passing it through is a defect)
    ("http://example.com/100%", "http://example.com/100%25"),
    ("http://example.com/a%4", "http://example.com/a%254"),
    ("http://example.com/%a%41", "http://example.com/%25aA"),
    # --- raw illegal characters are encoded ---
    ("http://example.com/my page.html", "http://example.com/my%20page.html"),
    ("http://example.com/über", "http://example.com/%C3%BCber"),
    # --- userinfo survives, IPv6 brackets are restored ---
    ("http://user:pw@example.com/x", "http://user:pw@example.com/x"),
    ("http://[::1]:8080/x", "http://[::1]:8080/x"),
    # --- surrounding whitespace is stripped ---
    ("  http://example.com/x  ", "http://example.com/x"),
]


@pytest.mark.parametrize(("raw", "expected"), NORMALIZE_CASES)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_strips_fragment() -> None:
    """The CLAUDE.md-documented invocation must keep working."""
    assert normalize_url("http://localhost:3000/about#team") == (
        "http://localhost:3000/about"
    )


@pytest.mark.parametrize(("raw", "expected"), NORMALIZE_CASES)
def test_normalize_is_idempotent(raw: str, expected: str) -> None:
    once = normalize_url(raw)
    assert normalize_url(once) == once
    assert once == expected


# Cases where a plausible implementation is wrong, and *how* it is wrong.
NAIVE_WRONG = [
    # posixpath.normpath collapses interior "//" — that merges two distinct pages
    ("http://example.com//a//b", "http://example.com/a/b"),
    # ...but preserves a leading "//", which is also wrong (normpath gives "//x")
    ("http://example.com//../x", "http://example.com//x"),
    # normpath drops the trailing slash that a trailing "." or ".." implies
    ("http://example.com/a/b/..", "http://example.com/a"),
    ("http://example.com/a/b/.", "http://example.com/a/b"),
    # lowering the path would merge case-sensitive resources
    ("http://example.com/About", "http://example.com/about"),
    # stripping trailing slashes would merge "/about/" and "/about"
    ("http://example.com/about/", "http://example.com/about"),
    # dropping the port unconditionally would merge different origins
    ("http://example.com:443/x", "http://example.com/x"),
    # decoding %2F would turn one segment into two
    ("http://example.com/a%2Fb", "http://example.com/a/b"),
    # decoding %2E%2E would let a path escape
    ("http://example.com/a/%2E%2E/b", "http://example.com/b"),
]


@pytest.mark.parametrize(("raw", "wrong"), NAIVE_WRONG)
def test_naive_implementation_would_differ(raw: str, wrong: str) -> None:
    """Guard the cases where the obvious implementation is silently wrong."""
    assert normalize_url(raw) != wrong


@pytest.mark.parametrize(
    "raw",
    [
        "localhost:3000",  # parses as scheme "localhost" — a very common typo
        "mailto:someone@example.com",
        "javascript:void(0)",
        "tel:+1234567890",
        "data:text/html,<p>hi</p>",
        "ftp://example.com/",
        "file:///etc/hosts",
        "not a url",
        "",
        "   ",
        "http://",  # no host
        "http://example.com:99999/",  # port out of range
    ],
)
def test_invalid_urls_raise(raw: str) -> None:
    with pytest.raises(InvalidURL):
        normalize_url(raw)


def test_invalid_url_is_a_value_error() -> None:
    assert issubclass(InvalidURL, ValueError)


def test_stray_percent_cannot_form_an_escape_on_a_later_pass() -> None:
    """Regression: "%a%41" normalized to "%aA", whose "aA" then read as an
    escape and became "%AA" — a different URL on the second pass."""
    once = normalize_url("http://example.com/%a%41")

    assert once == "http://example.com/%25aA"
    assert normalize_url(once) == once


@pytest.mark.parametrize(
    "raw",
    [
        "http://[::1",  # unterminated bracket
        "http://[::1]ü/",
        "http://[/x",
        "http://[",
    ],
)
def test_malformed_ipv6_raises_invalid_url(raw: str) -> None:
    """Regression: urlsplit raises a bare ValueError on these, and callers
    (`resolve_href`, `is_internal`) only catch InvalidURL — so it leaked."""
    with pytest.raises(InvalidURL):
        normalize_url(raw)


# --- resolve_href -----------------------------------------------------------


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/about", "http://example.com/about"),
        # a relative href replaces the base's last segment ("/a" -> "/about")
        ("about", "http://example.com/about"),
        ("../up", "http://example.com/up"),
        ("?q=1", "http://example.com/a?q=1"),
        # a pure fragment points at the page itself, minus the fragment
        ("#frag", "http://example.com/a"),
        # absolute hrefs win over the base, including cross-origin ones
        ("https://other.com/abs", "https://other.com/abs"),
        ("/about#team", "http://example.com/about"),
    ],
)
def test_resolve_href(href: str, expected: str) -> None:
    assert resolve_href(href, "http://example.com/a") == expected


@pytest.mark.parametrize(
    "href",
    [
        "",
        "   ",
        "mailto:someone@example.com",
        "javascript:void(0)",
        "tel:+1234567890",
        "data:text/html,<p>hi</p>",
    ],
)
def test_resolve_href_returns_none_for_unusable(href: str) -> None:
    """A crawler sees these constantly and none of them are errors."""
    assert resolve_href(href, "http://example.com/a") is None


# --- origins ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://example.com/x", Origin("http", "example.com", None)),
        ("http://example.com:80/x", Origin("http", "example.com", None)),
        ("https://example.com:443/x", Origin("https", "example.com", None)),
        ("http://example.com:8080/x", Origin("http", "example.com", 8080)),
        # userinfo is not part of the origin
        ("http://user:pw@example.com/x", Origin("http", "example.com", None)),
        # host case folds
        ("http://EXAMPLE.com/x", Origin("http", "example.com", None)),
    ],
)
def test_origin_of(url: str, expected: Origin) -> None:
    assert origin_of(url) == expected


def test_origin_str() -> None:
    assert str(Origin("http", "example.com", None)) == "http://example.com"
    assert str(Origin("http", "example.com", 8080)) == "http://example.com:8080"
    assert str(Origin("http", "::1", 8080)) == "http://[::1]:8080"


@pytest.mark.parametrize(
    ("url", "internal"),
    [
        ("http://example.com/other", True),
        ("http://example.com/deep/path?q=1", True),
        ("http://example.com:80/other", True),  # :80 is the default
        ("http://user:pw@example.com/other", True),  # same origin
        ("https://example.com/x", False),  # scheme differs
        ("http://example.com:8080/x", False),  # port differs
        ("http://other.com/x", False),
        ("http://sub.example.com/x", False),  # subdomains are not the origin
        ("mailto:someone@example.com", False),
        ("garbage", False),  # unusable URLs are never internal
    ],
)
def test_is_internal(url: str, internal: bool) -> None:
    assert is_internal(url, origin_of("http://example.com/")) is internal


# --- route shapes -----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "shape"),
    [
        # identifiers fold
        ("http://example.com/courses/123", "http://example.com/courses/:id"),
        ("http://example.com/courses/123/edit", "http://example.com/courses/:id/edit"),
        ("http://example.com/2019/03/hello", "http://example.com/:id/:id/hello"),
        (
            "http://example.com/orders/550e8400-e29b-41d4-a716-446655440000",
            "http://example.com/orders/:id",
        ),
        ("http://example.com/items?page=2", "http://example.com/items?page=:id"),
        (
            "http://example.com/courses/12?page=3&sort=name",
            "http://example.com/courses/:id?page=:id&sort=name",
        ),
        # a trailing slash is a different route, as it is a different page
        ("http://example.com/courses/12/", "http://example.com/courses/:id/"),
        # ...and so is the host
        ("http://other.com/courses/12", "http://other.com/courses/:id"),
        # names do not fold
        ("http://example.com/courses/abc", "http://example.com/courses/abc"),
        ("http://example.com/users/alice", "http://example.com/users/alice"),
        ("http://example.com/search?q=foo", "http://example.com/search?q=foo"),
        # a version is not a row
        ("http://example.com/v2/courses", "http://example.com/v2/courses"),
        # nothing to fold
        ("http://example.com/", "http://example.com/"),
        ("http://example.com/about", "http://example.com/about"),
        # an escape that is not a separator stays one segment
        ("http://example.com/a%2Fb", "http://example.com/a%2Fb"),
    ],
)
def test_url_shape(url: str, shape: str) -> None:
    assert url_shape(url) == shape


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # The rule has to stay narrow enough to keep these apart. A rule broad
        # enough to catch /users/alice would merge every top-level page.
        ("http://example.com/about", "http://example.com/contact"),
        ("http://example.com/login", "http://example.com/logout"),
        ("http://example.com/courses/new", "http://example.com/courses/edit"),
        # "new" is part of the route, not a row key
        ("http://example.com/courses/new", "http://example.com/courses/7"),
    ],
)
def test_shapes_that_must_stay_apart(left: str, right: str) -> None:
    assert url_shape(left) != url_shape(right)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/courses/123",
        "http://example.com/courses/123/edit",
        "http://example.com/orders/550e8400-e29b-41d4-a716-446655440000",
        "http://example.com/items?page=2",
        "http://example.com/about",
        "http://example.com/courses/12/",
    ],
)
def test_shaping_is_idempotent(url: str) -> None:
    """A shape is not a page, but it is stable — feeding one back changes
    nothing, so nothing downstream can oscillate."""
    once = url_shape(url)
    assert url_shape(once) == once


def test_a_shape_is_not_a_url_anyone_can_visit() -> None:
    assert url_shape("http://example.com/courses/123") != (
        "http://example.com/courses/123"
    )


# --- skip rules -------------------------------------------------------------


@pytest.mark.parametrize(
    ("patterns", "url", "skipped"),
    [
        # a literal covers the path and everything under it...
        (["/admin"], "http://h/admin", True),
        (["/admin"], "http://h/admin/", True),
        (["/admin"], "http://h/admin/users", True),
        (["/admin"], "http://h/admin?tab=1", True),
        # ...but only across a whole segment
        (["/admin"], "http://h/administrators", False),
        (["/admin"], "http://h/admin-panel", False),
        (["/admin"], "http://h/user/admin", False),
        # a trailing slash says the same thing explicitly
        (["/admin/"], "http://h/admin/users", True),
        (["/admin/"], "http://h/admin/x", True),
        # a regex reaches what a literal cannot
        ([r"re:\.pdf$"], "http://h/files/q3.pdf", True),
        ([r"re:\.pdf$"], "http://h/report.html", False),
        ([r"re:/page/\d+"], "http://h/page/12", True),
        ([r"re:page=\d+"], "http://h/x?page=12", True),
        ([r"re:^/search"], "http://h/search?q=1", True),
        ([r"re:^/search"], "http://h/deep/search", False),
        # several patterns are a union
        (["/admin", r"re:\.pdf$"], "http://h/admin", True),
        (["/admin", r"re:\.pdf$"], "http://h/a.pdf", True),
        (["/admin", r"re:\.pdf$"], "http://h/about", False),
        # the host is not part of what is matched
        (["/admin"], "http://other.example/admin", True),
        # a literal is matched as text, not as a pattern
        (["/report.html"], "http://h/reportXhtml", False),
        (["/report.html"], "http://h/report.html", True),
    ],
)
def test_skip_rules(patterns: list[str], url: str, skipped: bool) -> None:
    assert SkipRules(patterns).matches(url) is skipped


def test_no_patterns_skips_nothing() -> None:
    rules = SkipRules()

    assert not rules
    assert rules.matches("http://h/anything") is False


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        # A pattern that can never match is rejected rather than silently
        # matching nothing: a path always starts with "/".
        ("admin", "cannot match"),
        ("http://example.com/admin", "cannot match"),  # a URL, not a path
        ("re:[unclosed", "not a valid regex"),
        ("re:(?P<nothing", "not a valid regex"),
    ],
)
def test_unusable_skip_patterns_are_rejected(pattern: str, expected: str) -> None:
    with pytest.raises(InvalidSkipPattern) as excinfo:
        SkipRules([pattern])

    assert expected in str(excinfo.value)


def test_a_rejected_skip_pattern_says_what_to_write_instead() -> None:
    """Including for someone who pasted a whole URL, which is the easy
    mistake."""
    with pytest.raises(InvalidSkipPattern) as excinfo:
        SkipRules(["http://example.com/admin"])

    message = str(excinfo.value)
    assert "'/admin'" in message, message
    assert "'re:http://example.com/admin'" in message, message


# --- loopback ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://localhost:3000/", True),
        ("http://LOCALHOST/", True),  # host case folds before this is asked
        ("http://127.0.0.1/", True),
        ("http://127.0.0.1:8080/", True),
        # 127.0.0.0/8 is loopback, not just the one address
        ("http://127.9.9.9/", True),
        ("http://[::1]:3000/", True),
        # a dev server on the LAN is still someone else's machine
        ("http://192.168.1.5:3000/", False),
        ("http://10.0.0.4/", False),
        ("http://0.0.0.0:3000/", False),  # a wildcard bind, not an address
        ("https://example.com/", False),
        # "localhost" has to match as a name, not as a prefix or a suffix
        ("http://localhost.example.com/", False),
        ("http://notlocalhost/", False),
    ],
)
def test_is_loopback(url: str, local: bool) -> None:
    assert is_loopback(origin_of(url)) is local


# --- internal_links ---------------------------------------------------------


def test_internal_links_filters_dedupes_and_preserves_order() -> None:
    origin = origin_of("http://example.com/")
    hrefs = [
        "/about",
        "http://example.com/about",  # same node, absolute spelling
        "/about#team",  # same node after the fragment is dropped
        "https://elsewhere.com/",  # off-origin
        "http://example.com:8080/x",  # off-origin by port
        "mailto:someone@example.com",  # not a page
        "/",  # the base itself
        "/courses?b=2&a=1",
        "/courses?a=1&b=2",  # a genuinely distinct node
        "/about",  # duplicate again
    ]

    assert internal_links("http://example.com/", hrefs, origin) == [
        "http://example.com/about",
        "http://example.com/courses?b=2&a=1",
        "http://example.com/courses?a=1&b=2",
    ]


def test_internal_links_drops_self_link_from_bare_host() -> None:
    """The root would otherwise duplicate itself: "/" vs a host with no path."""
    origin = origin_of("http://example.com")
    assert internal_links("http://example.com", ["/", "/#top"], origin) == []


def test_internal_links_preserves_document_order() -> None:
    origin = origin_of("http://example.com/")
    assert internal_links(
        "http://example.com/", ["/c", "/a", "/b"], origin
    ) == [
        "http://example.com/c",
        "http://example.com/a",
        "http://example.com/b",
    ]


def test_internal_links_on_empty_input() -> None:
    assert internal_links("http://example.com/", [], origin_of("http://example.com/")) == []
