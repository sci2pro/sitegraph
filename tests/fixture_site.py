"""An in-process HTTP site used to exercise the crawler end to end.

It is a plain `http.server` on an ephemeral port, so a test that uses it is
testing the real stack — real Chromium, real HTTP, real normalization — rather
than a mock of our own assumptions.

`PAGES` is shaped to hit the cases where a crawler is most likely to be subtly
wrong: fragments that must collapse, query strings that must not, a link that
is off-origin, a 404 that must still appear in the graph, and a duplicate link
that must produce exactly one edge.

`run_site` can also serve a *signed-in* site: pass ``cookie`` and ``protected``
and the listed paths redirect to a login page until that cookie is present,
which is how a real app behaves and what `login`/`--storage-state` exist for.
"""

from __future__ import annotations

import itertools
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

__all__ = ["PAGES", "run_site"]


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>"
    )


#: Keyed by request target *including* the query string, since the query is
#: what distinguishes /search?q=foo from /search?q=bar.
PAGES: dict[str, str] = {
    "/": _page(
        "Home",
        """
        <a href="/about">About</a>
        <a href="/about/">About with slash</a>
        <a href="/courses">Courses</a>
        <a href="/login">Login</a>
        <a href="/about#team">About, fragment</a>
        <a href="#top">Self, fragment only</a>
        <a href="mailto:someone@example.com">Mail</a>
        <a href="javascript:void(0)">Script</a>
        <a href="https://example.com/external">Off-origin</a>
        <a href="/boom">Boom</a>
        """,
    ),
    "/about": _page(
        "About",
        """
        <a href="/">Home</a>
        <a href="/courses/123">Course 123</a>
        <a href="/missing">Broken</a>
        <a href="/about">Self link</a>
        """,
    ),
    "/about/": _page("About slash", "<a href='/'>Home</a>"),
    "/courses": _page(
        "Courses",
        """
        <a href="/courses/123">Course 123</a>
        <a href="/courses/new">New course</a>
        <a href="/search?q=foo">Search foo</a>
        <a href="/search?q=bar">Search bar</a>
        <a href="/courses/123">Course 123 again</a>
        """,
    ),
    "/courses/123": _page("Course 123", "<a href='/courses'>Courses</a>"),
    "/courses/new": _page("New course", "<a href='/courses'>Courses</a>"),
    "/login": _page("Login", "<p>No links here.</p>"),
    "/search?q=foo": _page("Search: foo", "<a href='/'>Home</a>"),
    "/search?q=bar": _page("Search: bar", "<a href='/'>Home</a>"),
    # Deliberately absent, so a request for it 404s:
    # "/missing"
    # Deliberately broken, so a request for it fails to render at all — see
    # BROKEN_PATH in the handler.
}

#: Served by dropping the connection with no response, which is the closest a
#: test server gets to "the page failed to render": the browser reports a
#: network error, not an HTTP status, so the crawler has to record a page with
#: no status and no screenshot rather than treating it as a 4xx.
BROKEN_PATH = "/boom"

#: Visiting this path sets the session cookie. A real app would check a
#: password first; the crawler only ever cares about what the browser ends up
#: holding, so the cookie alone is what the fixture has to produce.
LOGIN_PATH = "/login"

#: A three-page signed-in site: /private bounces to /login unless the session
#: cookie is present, which is the behaviour `--storage-state` has to defeat.
AUTH_PAGES: dict[str, str] = {
    "/": _page("Home", "<a href='/private'>Private</a>"),
    "/private": _page("Private", "<a href='/'>Home</a>"),
    LOGIN_PATH: _page("Sign in", "<a href='/'>Home</a>"),
}

AUTH_COOKIE = "sessionid"

#: A rotating fixture rolls its session over every this many authenticated
#: requests.
ROTATION_EVERY = 3

#: How many tokens it still accepts. The window has to exceed the rotations
#: that can happen while a request is in flight — a real rotating-session
#: implementation keeps the old key briefly for exactly that reason — but stay
#: small enough that a browser which never picks up a rotation is eventually
#: logged out, which is the behaviour these fixtures exist to produce.
ROTATION_GRACE = 4


def _handler(
    pages: dict[str, str],
    cookie: str | None = None,
    protected: frozenset[str] = frozenset(),
    rotate: bool = False,
) -> type[BaseHTTPRequestHandler]:
    # The tokens still accepted, newest last. Only used in `rotate` mode.
    #
    # A grace window rather than "one live token", because that is what real
    # rotating-session implementations do: they keep the previous key alive
    # briefly so that a request already in flight is not rejected. Rotating
    # with no grace would log out anyone with two tabs open, so it is not a
    # thing an app survives having, and a fixture that modelled it would be
    # testing a server nobody runs.
    live: deque[str] = deque(maxlen=ROTATION_GRACE)
    issued = itertools.count(1)
    seen = itertools.count(1)
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _sent_token(self) -> str | None:
            header = self.headers.get("Cookie") or ""
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == cookie:
                    return value
            return None

        def _signed_in(self) -> bool:
            if cookie is None:
                return False
            token = self._sent_token()
            if token is None:
                return False
            if not rotate:
                # The plain fixture only cares that the cookie is there.
                return True
            with lock:
                return token in live

        def _issue(self) -> str:
            with lock:
                token = f"t{next(issued)}"
                live.append(token)
            return token

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            if self.path == BROKEN_PATH:
                self.close_connection = True
                return

            if self.path in protected and not self._signed_in():
                self.send_response(302)
                self.send_header("Location", LOGIN_PATH)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            body = pages.get(self.path)
            if body is None:
                body = _page("Not found", f"<p>No page at {self.path}</p>")
                self.send_response(404)
            else:
                self.send_response(200)
            payload = body.encode("utf-8")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            if cookie is not None and self.path == LOGIN_PATH:
                self.send_header(
                    "Set-Cookie", f"{cookie}={self._issue() if rotate else 'signed-in'}; Path=/"
                )
            elif rotate and self._signed_in():
                # Rolls the session over periodically. The token just used
                # stays valid until it falls out of the window, so a request
                # already in flight is not punished for the race.
                with lock:
                    due = next(seen) % ROTATION_EVERY == 0
                if due:
                    self.send_header("Set-Cookie", f"{cookie}={self._issue()}; Path=/")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            """Keep pytest's output clean."""

    return Handler


@contextmanager
def run_site(
    pages: dict[str, str] | None = None,
    *,
    cookie: str | None = None,
    protected: Iterable[str] = (),
    rotate: bool = False,
) -> Iterator[str]:
    """Serve *pages* (default `PAGES`) and yield its base URL.

    The URL looks like ``http://127.0.0.1:53421``, on an ephemeral port.

    Pass *cookie* and *protected* to require a session: those paths redirect to
    the login page (which sets *cookie*) until the cookie is present. Add
    *rotate* to make it a site that rolls its session cookie over on every
    authenticated request and retires the old one — the case where two browsers
    with separate cookie jars fall out of step.

    Bound to 127.0.0.1 rather than localhost on purpose: ``localhost`` can
    resolve to ::1 while the server is listening on IPv4, which would make the
    crawl fail for reasons that have nothing to do with the crawler.
    """
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _handler(pages or PAGES, cookie, frozenset(protected), rotate)
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
