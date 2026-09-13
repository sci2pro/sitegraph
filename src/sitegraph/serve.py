"""Serve previously crawled results as an interactive explorer.

Reads only. This module never imports the crawler and never touches a browser —
spec §8 is explicit that `serve` must not crawl, and the separation is what
makes the two commands independently safe: `serve` on a directory from last
week is just as valid as `serve` on one from a minute ago.

The server binds to the loopback interface on purpose. A crawl of an internal
app can easily contain things that should not be readable from the rest of the
network, and "local-first" (spec §1) is a promise about where this data goes.
"""

from __future__ import annotations

import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sitegraph.store import (
    GRAPH_FILE,
    PAGES_DIR,
    SCREENSHOTS_DIR,
    CrawlNotFound,
    load_graph,
    page_filename,
    screenshot_filename,
)

__all__ = ["HOST", "UIServer", "make_server", "serve"]

HOST = "127.0.0.1"

#: The static assets, by URL path. Mapped by an explicit table rather than by
#: joining the request path onto a directory, so no request can name a file
#: that is not one of these three.
_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

UI_DIR = Path(__file__).parent / "ui"

# Requests are percent-decoded *before* matching, and both patterns are
# anchored and allowlist-only, so a traversal attempt ("/pages/../../etc/passwd")
# simply fails to match rather than being sanitized.
_PAGE_ROUTE = re.compile(r"\A/pages/(?P<id>\d{1,12})\.json\Z")
_SHOT_ROUTE = re.compile(r"\A/screenshots/(?P<name>[0-9A-Za-z._-]{1,80}\.webp)\Z")


class _Handler(BaseHTTPRequestHandler):
    """Routes the UI's assets and the crawl's data; 404 for everything else."""

    server_version = "sitegraph"
    protocol_version = "HTTP/1.1"
    #: Set by `_make_handler`. A class attribute rather than a constructor
    #: argument because `BaseHTTPRequestHandler.__init__` accepts neither.
    directory: Path

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = unquote(urlsplit(self.path).path)

        if path in _ASSETS:
            name, content_type = _ASSETS[path]
            self._send_file(UI_DIR / name, content_type)
            return

        if path == f"/{GRAPH_FILE}":
            self._send_file(self.directory / GRAPH_FILE, "application/json")
            return

        match = _PAGE_ROUTE.match(path)
        if match:
            record = self.directory / PAGES_DIR / page_filename(match["id"])
            self._send_file(record, "application/json")
            return

        match = _SHOT_ROUTE.match(path)
        if match:
            shot = self.directory / SCREENSHOTS_DIR / screenshot_filename(
                # The route allows a filename the crawler would never write, so
                # the id is re-derived rather than trusted: "000012.webp" here
                # and nothing else.
                match["name"][: -len(".webp")]
            )
            self._send_file(shot, "image/webp")
            return

        self._not_found(f"no route for {path}")

    def do_HEAD(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self.do_GET()

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._not_found(f"{path.name} is not present in this crawl")
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # No caching anywhere: a re-crawl reuses the same filenames, so a
        # cached screenshot would show the previous crawl's pixels under the
        # new crawl's node. Re-reading a local file is free.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_found(self, message: str) -> None:
        body = (message + "\n").encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        print(f"  404  {urlsplit(self.path).path}")

    def log_message(self, *args: object) -> None:
        """Silence the per-request log; a screenshot grid is hundreds of lines."""


def _make_handler(directory: Path) -> type[_Handler]:
    """Return a handler class that serves data out of *directory*."""

    class BoundHandler(_Handler):
        pass

    BoundHandler.directory = Path(directory)
    return BoundHandler


class UIServer(ThreadingHTTPServer):
    """A threaded server with the crawl directory attached."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], directory: Path) -> None:
        self.directory = Path(directory)
        super().__init__(address, _make_handler(self.directory))


def make_server(directory: Path, port: int = 0, host: str = HOST) -> UIServer:
    """Build (but do not start) a server for *directory*.

    Exposed mainly so tests can drive a real server on an ephemeral port; the
    CLI goes through `serve`.
    """
    return UIServer((host, port), Path(directory))


def serve(directory: Path, port: int) -> None:
    """Serve the crawl data in *directory* over HTTP on *port*.

    Reads previously generated data only — this must never crawl.
    """
    try:
        graph = load_graph(directory)
    except CrawlNotFound as exc:
        raise SystemExit(
            f"error: {exc}\n"
            f"       run `sitegraph crawl <url> --output {directory}` first."
        ) from None

    nodes = graph.get("nodes") or []
    try:
        server = make_server(directory, port)
    except OSError as exc:
        raise SystemExit(
            f"error: cannot listen on port {port}: {exc}\n"
            f"       another process may be using it — try --port {port + 1}."
        ) from None

    bound = server.server_address[1]
    pages = len(nodes)
    print(
        f"sitegraph — {pages} page{'' if pages == 1 else 's'}, "
        f"{len(graph.get('edges') or [])} link(s)"
    )
    print(f"  http://localhost:{bound}   (local only — Ctrl-C to stop)")
    if not pages:
        print("  the crawl found no pages; the explorer will be empty.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
