"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sitegraph.store import GRAPH_FILE, PAGES_DIR, SCREENSHOTS_DIR, THUMBS_DIR

#: A valid crawl of a three-page site, one of which failed to render. Written
#: by hand rather than produced by a crawl so that tests of the *reader* do not
#: depend on the writer.
#:
#: ``thumb`` is present on the two rendered pages and absent on the failed one,
#: which is exactly how the writer emits it — a page with no picture of its own
#: has no card-sized copy either.
GRAPH = {
    "root": "000001",
    "nodes": [
        {
            "id": "000001",
            "url": "http://example.com/",
            "title": "Home",
            "depth": 0,
            "status": 200,
            "failed": False,
            "screenshot": "screenshots/000001.webp",
            "thumb": "thumbs/000001.webp",
        },
        {
            "id": "000002",
            "url": "http://example.com/about",
            "title": "About",
            "depth": 1,
            "status": 200,
            "failed": False,
            "screenshot": "screenshots/000002.webp",
            "thumb": "thumbs/000002.webp",
        },
        {
            "id": "000003",
            "url": "http://example.com/gone",
            "title": "",
            "depth": 1,
            "status": None,
            "failed": True,
            "screenshot": None,
        },
    ],
    "edges": [
        {"source": "000001", "target": "000002"},
        {"source": "000002", "target": "000001"},
        {"source": "000001", "target": "000003"},
    ],
}

PAGES = {
    "000001": {
        "id": "000001",
        "url": "http://example.com/",
        "title": "Home",
        "status": 200,
        "depth": 0,
        "screenshot": "screenshots/000001.webp",
        "failed": False,
        "error": None,
        "links": ["http://example.com/about", "http://example.com/gone"],
    },
    "000002": {
        "id": "000002",
        "url": "http://example.com/about",
        "title": "About",
        "status": 200,
        "depth": 1,
        "screenshot": "screenshots/000002.webp",
        "failed": False,
        "error": None,
        "links": ["http://example.com/"],
    },
    "000003": {
        "id": "000003",
        "url": "http://example.com/gone",
        "title": "",
        "status": None,
        "depth": 1,
        "screenshot": None,
        "failed": True,
        "error": "net::ERR_CONNECTION_REFUSED",
        "links": [],
    },
}

#: Smallest possible WebP container (VP8L, 1x1). Only the first bytes matter to
#: the tests that check a screenshot is reachable and typed correctly.
TINY_WEBP = bytes.fromhex(
    "524946461a000000574542505650384c0d0000002f0000001007000010070000"
    "0106000000"
)


@pytest.fixture
def crawl_dir(tmp_path: Path) -> Path:
    """A minimal, valid crawl output directory."""
    directory = tmp_path / ".sitegraph"
    (directory / PAGES_DIR).mkdir(parents=True)
    (directory / SCREENSHOTS_DIR).mkdir(parents=True)
    (directory / THUMBS_DIR).mkdir(parents=True)
    (directory / GRAPH_FILE).write_text(json.dumps(GRAPH), encoding="utf-8")

    for node_id, record in PAGES.items():
        (directory / PAGES_DIR / f"{node_id}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    for node_id in ("000001", "000002"):
        (directory / SCREENSHOTS_DIR / f"{node_id}.webp").write_bytes(TINY_WEBP)
        (directory / THUMBS_DIR / f"{node_id}.webp").write_bytes(TINY_WEBP)

    return directory
