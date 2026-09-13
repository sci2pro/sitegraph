"""Helpers for driving a real sitegraph server from tests."""

from __future__ import annotations

import http.client
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

from sitegraph.serve import make_server

__all__ = ["request", "running"]


@contextmanager
def running(directory: Path) -> Iterator[int]:
    """Serve *directory* on an ephemeral port and yield the port."""
    server = make_server(directory, 0)
    # shutdown() waits for the serve loop to notice, and the default poll
    # interval is 0.5s. These tests start and stop dozens of servers, so the
    # default would spend most of the suite's wall clock doing nothing.
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port: int, path: str, method: str = "GET"):
    """Send *path* verbatim — no client-side dot-segment normalization."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        connection.close()
