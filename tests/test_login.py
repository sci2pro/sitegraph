"""Tests for signed-in crawling.

`login` is interactive by nature — it exists precisely because a human has to
do the authenticating — so the tests stand in for the human by passing their
own `confirm`. Everything else is the real path: a real Chromium, a real
session cookie, a real storage-state file handed back to `crawl`.

The pair of tests that matter most are `..._reaches_protected_pages` and
`..._silently_captures_the_login_page` below. The second is the failure mode a
user will actually hit, and it is important that it is *observable* rather than
a crash: without a session the crawler does not fail, it quietly records the
login page under the protected URL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixture_site import AUTH_COOKIE, AUTH_PAGES, LOGIN_PATH, _page, run_site
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from sitegraph.crawl import crawl
from sitegraph.login import describe, login
from sitegraph.store import GRAPH_FILE

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module", autouse=True)
def browser_installed() -> None:
    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
    except Exception as exc:  # noqa: BLE001 - a missing install raises several types
        pytest.skip(f"chromium is not installed ({exc})")


@pytest.fixture
def auth_site():
    """A site whose /private page requires a session cookie."""
    with run_site(AUTH_PAGES, cookie=AUTH_COOKIE, protected={"/private"}) as base:
        yield base


def sign_in(base: str, path: Path) -> Path:
    """Run `login` to completion without a human, as the tests' stand-in."""
    login(
        f"{base}{LOGIN_PATH}",
        path,
        headless=True,
        confirm=lambda page: None,  # "yes, I'm signed in" the moment it loads
    )
    return path


def titles_by_path(directory: Path) -> dict[str, str]:
    from urllib.parse import urlsplit

    graph = json.loads((directory / GRAPH_FILE).read_text(encoding="utf-8"))
    return {
        urlsplit(node["url"]).path: node["title"] for node in graph["nodes"]
    }


# --- the login helper ---------------------------------------------------


def test_login_saves_a_usable_session(auth_site: str, tmp_path: Path) -> None:
    path = sign_in(auth_site, tmp_path / "session.json")

    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["cookies"], "no cookies were captured"
    assert state["cookies"][0]["name"] == AUTH_COOKIE
    assert state["cookies"][0]["value"] == "signed-in"


def test_login_reports_where_the_session_landed(auth_site: str, tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    session = login(
        f"{auth_site}{LOGIN_PATH}",
        path,
        headless=True,
        confirm=lambda page: None,
    )

    assert session.cookies == 1
    assert session.status == 200
    assert session.storage_state == path
    assert session.final_url == f"{auth_site}{LOGIN_PATH}"
    assert "1 cookie" in describe(session)


def test_login_creates_the_sessions_parent_directory(
    auth_site: str, tmp_path: Path
) -> None:
    path = tmp_path / "nested" / "deeper" / "session.json"
    sign_in(auth_site, path)

    assert path.is_file()


def test_login_rejects_an_unusable_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        login("localhost:3000", tmp_path / "session.json", headless=True)


# --- crawling with a session --------------------------------------------


def test_crawl_with_a_session_reaches_protected_pages(
    auth_site: str, tmp_path: Path
) -> None:
    session = sign_in(auth_site, tmp_path / "session.json")
    output = tmp_path / "signed-in"
    crawl(auth_site, output, 20, storage_state=session)

    titles = titles_by_path(output)
    assert titles["/private"] == "Private"
    assert titles["/"] == "Home"


def test_crawl_without_a_session_silently_captures_the_login_page(
    auth_site: str, tmp_path: Path
) -> None:
    """The failure mode to be aware of: the crawl does not error, it records
    the redirect target's content under the protected URL."""
    output = tmp_path / "signed-out"
    crawl(auth_site, output, 20)

    titles = titles_by_path(output)
    assert titles["/private"] == "Sign in"
    assert "/private" in titles, "the page is still visited, just not really seen"


def test_the_session_is_what_changes_the_outcome(
    auth_site: str, tmp_path: Path
) -> None:
    """Same URL, same crawler, two sessions, two different pages."""
    session = sign_in(auth_site, tmp_path / "session.json")
    signed_in = tmp_path / "a"
    signed_out = tmp_path / "b"
    crawl(auth_site, signed_in, 20, storage_state=session)
    crawl(auth_site, signed_out, 20)

    assert titles_by_path(signed_in)["/private"] != titles_by_path(signed_out)["/private"]


def test_a_session_for_a_dead_file_is_an_error_not_a_silent_crawl(tmp_path: Path) -> None:
    """A typo in the path must not quietly produce a signed-out crawl — and
    must not leave a half-made output directory behind either."""
    output = tmp_path / "out"
    with run_site(AUTH_PAGES, cookie=AUTH_COOKIE, protected={"/private"}) as base:
        with pytest.raises(FileNotFoundError) as excinfo:
            crawl(base, output, 5, storage_state=tmp_path / "nope.json")

    assert "sitegraph login" in str(excinfo.value)
    assert not output.exists(), "a failed start should write nothing at all"


def test_a_failed_start_leaves_no_driver_behind(tmp_path: Path) -> None:
    """Regression: `ChromiumRenderer.__enter__` used to leak the driver when it
    raised, and a leaked driver leaves a running event loop that breaks every
    subsequent crawl in the process with "Sync API inside the asyncio loop"."""
    from sitegraph.crawl import ChromiumRenderer

    broken = tmp_path / "broken.json"
    broken.write_text("{ this is not the storage state you are looking for", "utf-8")

    # Storage state that parses as a file but not as JSON: the failure lands
    # after Playwright has started, which is the case that used to leak.
    with pytest.raises(Exception):
        ChromiumRenderer(storage_state=broken).__enter__()

    # A fresh renderer must still start cleanly afterwards.
    with ChromiumRenderer() as renderer:
        assert renderer is not None


# --- one session across a pool of workers -------------------------------


#: Long enough that each of four workers fetches several pages, so a token
#: spent by one is dead by the time another comes to use the copy it holds.
ROTATING_PAGES = 12


@pytest.fixture
def rotating_site():
    """A site that retires its session cookie on every authenticated request.

    The worst case for a pool: the moment one browser spends the token, it is
    dead, so any other browser still holding a copy is shown the login page.
    Every page below is protected, and there are enough of them that a worker
    cannot get through the crawl on one token.
    """
    home = _page("Home", "".join(f"<a href='/p{i}'>p{i}</a>" for i in range(ROTATING_PAGES)))
    pages = {"/": home, LOGIN_PATH: AUTH_PAGES[LOGIN_PATH]}
    for index in range(ROTATING_PAGES):
        pages[f"/p{index}"] = _page(
            f"Page {index}", f"<a href='/p{(index + 1) % ROTATING_PAGES}'>next</a>"
        )
    protected = set(pages) - {LOGIN_PATH}
    with run_site(pages, cookie=AUTH_COOKIE, protected=protected, rotate=True) as base:
        yield base


def test_a_pooled_crawl_keeps_up_with_a_rotating_session(
    rotating_site: str, tmp_path: Path
) -> None:
    """The promise: --workers plus a session no longer records the login page
    under every URL after the first rotation. Without the sharing this is
    exactly what happens — the second worker to spend the token is bounced, and
    its page is captured as the login page under its own URL."""
    session_file = sign_in(rotating_site, tmp_path / "session.json")
    output = tmp_path / "pooled"
    crawl(rotating_site, output, 40, workers=4, storage_state=session_file)

    titles = titles_by_path(output)
    assert len(titles) == ROTATING_PAGES + 1
    for path, title in titles.items():
        assert title != "Sign in", f"{path} was captured as the login page"
    assert titles["/p0"] == "Page 0"
    assert titles[f"/p{ROTATING_PAGES - 1}"] == f"Page {ROTATING_PAGES - 1}"


def test_a_pooled_crawl_says_that_it_is_sharing_a_session(
    rotating_site: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    session_file = sign_in(rotating_site, tmp_path / "session.json")
    crawl(rotating_site, tmp_path / "out", 20, workers=3, storage_state=session_file)

    assert "sharing one session" in capsys.readouterr().out


def test_a_signed_out_crawl_has_no_session_to_share(
    rotating_site: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    crawl(rotating_site, tmp_path / "out", 3, workers=2)

    assert "sharing one session" not in capsys.readouterr().out


# --- the session file is a secret ---------------------------------------


def test_a_session_in_the_crawl_directory_is_not_served(
    auth_site: str, tmp_path: Path
) -> None:
    """`login` defaults to writing into `.sitegraph/`, so `serve` must not be
    able to hand those cookies to anything that can reach the port."""
    from serving import request, running

    output = tmp_path / ".sitegraph"
    session = sign_in(auth_site, output / "session.json")
    crawl(auth_site, output, 20, storage_state=session)

    with running(output) as port:
        status, _, body = request(port, "/session.json")

    assert status == 404
    assert b"signed-in" not in body


def test_the_crawler_never_writes_credentials_into_the_graph(
    auth_site: str, tmp_path: Path
) -> None:
    """Unlike `user:pass@` in the start URL, a storage state keeps the
    credential out of every recorded URL."""
    session = sign_in(auth_site, tmp_path / "session.json")
    output = tmp_path / "out"
    crawl(auth_site, output, 20, storage_state=session)

    text = (output / GRAPH_FILE).read_text(encoding="utf-8")
    assert "signed-in" not in text
    assert AUTH_COOKIE not in text
    assert "@" not in json.loads(text)["nodes"][0]["url"]
