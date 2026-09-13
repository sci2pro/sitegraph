"""An in-process HTTP site used to exercise the crawler end to end.

Shared by the crawl tests and by nothing else. It is a plain `http.server` on
an ephemeral port, so a test that uses it is testing the real stack — real
Chromium, real HTTP, real normalization — rather than a mock of our own
assumptions.

`PAGES` is shaped to hit the cases where a crawler is most likely to be subtly
wrong: fragments that must collapse, query strings that must not, a link that
is off-origin, a 404 that must still appear in the graph, and a duplicate link
that must produce exactly one edge.
"""

from __future__ import annotations

from collections.abc import Iterator
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


def _handler(pages: dict[str, str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            if self.path == BROKEN_PATH:
                self.close_connection = True
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
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            """Keep pytest's output clean."""

    return Handler


@contextmanager
def run_site(pages: dict[str, str] | None = None) -> Iterator[str]:
    """Serve *pages* (default `PAGES`) and yield its base URL.

    The URL looks like ``http://127.0.0.1:53421``, on an ephemeral port.

    Bound to 127.0.0.1 rather than localhost on purpose: ``localhost`` can
    resolve to ::1 while the server is listening on IPv4, which would make the
    crawl fail for reasons that have nothing to do with the crawler.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(pages or PAGES))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
