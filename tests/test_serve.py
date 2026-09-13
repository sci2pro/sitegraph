"""Tests for the results server.

The whole point of `serve` is that it is a *reader*: it hands back a directory
of files that another command wrote, and nothing else. The traversal cases and
the "never crawls" case below are the ones that matter.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
from pathlib import Path
from threading import Thread

import pytest
from serving import request, running

from sitegraph.serve import make_server, serve


# --- assets -------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html; charset=utf-8"),
        ("/index.html", "text/html; charset=utf-8"),
        ("/app.js", "text/javascript; charset=utf-8"),
        ("/styles.css", "text/css; charset=utf-8"),
    ],
)
def test_serves_the_ui(crawl_dir: Path, path: str, content_type: str) -> None:
    with running(crawl_dir) as port:
        status, served_type, body = request(port, path)

    assert status == 200
    assert served_type == content_type
    assert body


def test_index_references_its_assets(crawl_dir: Path) -> None:
    """A typo in an asset name would otherwise 404 silently in the browser."""
    with running(crawl_dir) as port:
        _, _, html = request(port, "/")
        text = html.decode()
        for asset in ("app.js", "styles.css"):
            assert asset in text
            assert request(port, f"/{asset}")[0] == 200


# --- crawl data ---------------------------------------------------------


def test_serves_graph_json(crawl_dir: Path) -> None:
    with running(crawl_dir) as port:
        status, content_type, body = request(port, "/graph.json")

    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body)["root"] == "000001"


def test_serves_page_records(crawl_dir: Path) -> None:
    with running(crawl_dir) as port:
        status, content_type, body = request(port, "/pages/000001.json")

    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body)["url"] == "http://example.com/"


def test_serves_screenshots(crawl_dir: Path) -> None:
    with running(crawl_dir) as port:
        status, content_type, body = request(port, "/screenshots/000001.webp")

    assert status == 200
    assert content_type == "image/webp"
    assert body[:4] == b"RIFF"


def test_every_graph_node_is_reachable(crawl_dir: Path) -> None:
    """The UI fetches these lazily, so a graph that points at nothing would
    only show up as broken images at runtime."""
    with running(crawl_dir) as port:
        graph = json.loads(request(port, "/graph.json")[2])
        for node in graph["nodes"]:
            assert request(port, f"/pages/{node['id']}.json")[0] == 200
            if node["screenshot"]:
                assert request(port, f"/{node['screenshot']}")[0] == 200


def test_responses_are_not_cached(crawl_dir: Path) -> None:
    """A re-crawl reuses filenames; a cached screenshot would be the previous
    crawl's pixels under the new crawl's node."""
    with running(crawl_dir) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", "/screenshots/000001.webp")
        response = connection.getresponse()
        response.read()
        assert response.getheader("Cache-Control") == "no-store"
        connection.close()


def test_head_requests_have_no_body(crawl_dir: Path) -> None:
    with running(crawl_dir) as port:
        status, content_type, body = request(port, "/graph.json", method="HEAD")

    assert status == 200
    assert content_type == "application/json"
    assert body == b""


# --- 404s ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/nope",
        "/pages/999999.json",
        "/screenshots/999999.webp",
        "/pages/notanid.json",
        "/pages/000001.json.bak",
        "/favicon.ico",
    ],
)
def test_unknown_paths_are_not_found(crawl_dir: Path, path: str) -> None:
    with running(crawl_dir) as port:
        assert request(port, path)[0] == 404


def test_missing_screenshot_is_not_found(crawl_dir: Path) -> None:
    """000003 failed to render, so it has no screenshot — and no thumbnail."""
    with running(crawl_dir) as port:
        assert request(port, "/screenshots/000003.webp")[0] == 404


def test_unsupported_method_is_rejected(crawl_dir: Path) -> None:
    with running(crawl_dir) as port:
        assert request(port, "/graph.json", method="POST")[0] in (405, 501)


# --- path traversal -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/pages/../../secret.txt",
        "/pages/%2e%2e%2f%2e%2e%2fsecret.txt",
        "/screenshots/..%2f..%2fsecret.txt",
        "/screenshots/../../../etc/passwd",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/pages/..%5c..%5csecret.txt",
    ],
)
def test_traversal_attempts_never_leak_files(crawl_dir: Path, path: str) -> None:
    secret = crawl_dir.parent / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")

    with running(crawl_dir) as port:
        status, _, body = request(port, path)

    assert status == 404, f"{path} was served"
    assert b"TOP SECRET" not in body
    assert b"root:" not in body


def test_asset_routes_are_a_fixed_table(crawl_dir: Path) -> None:
    """Assets can only ever be one of the three known files."""
    secret = Path(__file__).parent.parent / "pyproject.toml"
    assert secret.is_file()

    with running(crawl_dir) as port:
        status, _, body = request(port, "/..%2fpyproject.toml")

    assert status == 404
    assert b"hatchling" not in body


# --- startup ------------------------------------------------------------


def test_serve_reports_a_missing_crawl(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        serve(tmp_path / "never-crawled", 0)

    message = str(excinfo.value)
    assert "never-crawled" in message
    assert "sitegraph crawl" in message


def test_serve_reports_a_busy_port(crawl_dir: Path) -> None:
    with running(crawl_dir) as port:
        with pytest.raises(SystemExit) as excinfo:
            serve(crawl_dir, port)

    assert "--port" in str(excinfo.value)


def test_serve_stops_cleanly(crawl_dir: Path) -> None:
    """Ctrl-C must not print a traceback."""
    server = make_server(crawl_dir, 0)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

    assert not thread.is_alive()


# --- the read-only guarantee -------------------------------------------


def test_serve_does_not_import_the_crawler() -> None:
    """Spec §8: `serve` must never crawl. Importing the crawler would pull in
    Playwright, so this is checked in a fresh interpreter."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sitegraph.serve, sys; "
            "assert 'sitegraph.crawl' not in sys.modules, 'serve imported crawl'; "
            "assert 'playwright' not in sys.modules, 'serve pulled in playwright'; "
            "print('ok')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_serving_never_writes_to_the_crawl_directory(crawl_dir: Path) -> None:
    before = {path: path.stat().st_mtime_ns for path in crawl_dir.rglob("*")}

    with running(crawl_dir) as port:
        for path in (
            "/",
            "/app.js",
            "/styles.css",
            "/graph.json",
            "/pages/000001.json",
            "/screenshots/000001.webp",
            "/nope",
        ):
            request(port, path)

    after = {path: path.stat().st_mtime_ns for path in crawl_dir.rglob("*")}
    assert before == after


def test_binds_to_loopback_only(crawl_dir: Path) -> None:
    """A crawl of an internal app should not be readable from the network."""
    server = make_server(crawl_dir, 0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
