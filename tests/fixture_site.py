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

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

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


def _handler(
    pages: dict[str, str],
    cookie: str | None = None,
    protected: frozenset[str] = frozenset(),
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _cookies(self) -> dict[str, str]:
            header = self.headers.get("Cookie") or ""
            return dict(
                part.strip().split("=", 1) for part in header.split(";") if "=" in part
            )

        def _signed_in(self) -> bool:
            return cookie is not None and cookie in self._cookies()

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
                self.send_header("Set-Cookie", f"{cookie}=signed-in; Path=/")
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
) -> Iterator[str]:
    """Serve *pages* (default `PAGES`) and yield its base URL.

    The URL looks like ``http://127.0.0.1:53421``, on an ephemeral port.

    Pass *cookie* and *protected* to require a session: those paths redirect to
    the login page (which sets *cookie*) until the cookie is present.

    Bound to 127.0.0.1 rather than localhost on purpose: ``localhost`` can
    resolve to ::1 while the server is listening on IPv4, which would make the
    crawl fail for reasons that have nothing to do with the crawler.
    """
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _handler(pages or PAGES, cookie, frozenset(protected))
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
