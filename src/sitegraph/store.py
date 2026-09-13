"""The on-disk layout under the output directory.

This layout is a **public interface**, not an internal detail — `serve` and the
browser UI read it, and a user may reasonably look at it by hand::

    .sitegraph/
    ├── graph.json          # whole graph; independent of the per-page files
    ├── pages/000001.json   # one record per page
    └── screenshots/000001.webp

The filenames live here so that the writer (`crawl`) and the readers (`serve`,
the UI) cannot drift apart. Paths stored *inside* the data — a node's
``screenshot`` field — are relative to the output directory, never absolute, so
that a `serve` run rooted elsewhere still resolves them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sitegraph.graph import Graph

__all__ = [
    "GRAPH_FILE",
    "PAGES_DIR",
    "SCREENSHOTS_DIR",
    "SCREENSHOT_EXT",
    "CrawlStore",
    "CrawlNotFound",
    "load_graph",
    "page_filename",
    "screenshot_filename",
]

GRAPH_FILE = "graph.json"
PAGES_DIR = "pages"
SCREENSHOTS_DIR = "screenshots"
SCREENSHOT_EXT = "webp"


class CrawlNotFound(Exception):
    """Raised when a directory holds no crawl results to read."""


def page_filename(node_id: str) -> str:
    """Return the per-page record's filename for *node_id*."""
    return f"{node_id}.json"


def screenshot_filename(node_id: str) -> str:
    """Return the screenshot's filename for *node_id*."""
    return f"{node_id}.{SCREENSHOT_EXT}"


def _write_json(path: Path, payload: Any) -> None:
    """Write *payload* to *path*, atomically.

    The write goes to a temporary file in the same directory and is then moved
    into place with `os.replace`, which is atomic on POSIX and on Windows. The
    point is the interrupted-crawl guarantee in the spec: a crawl killed at any
    moment must leave a *parseable* ``graph.json`` behind, and a plain
    ``open().write()`` can be caught mid-flush with a truncated file, which
    would make the whole crawl unservable.

    No ``fsync``: the failure being defended against is a killed process, not a
    power cut, and the page cache survives the former. Paying a flush per file
    would add seconds to a large crawl to cover a case this tool does not meet.
    """
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def load_graph(directory: Path) -> dict:
    """Read and return ``graph.json`` from *directory*.

    Raises `CrawlNotFound` if it is missing or unreadable, which is the common
    mistake — running ``serve`` before ever running ``crawl``.
    """
    path = Path(directory) / GRAPH_FILE
    if not path.is_file():
        raise CrawlNotFound(
            f"no crawl results in {directory!s}: {GRAPH_FILE} not found"
        )
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CrawlNotFound(f"could not read {path}: {exc}") from exc


class CrawlStore:
    """Writes a crawl's output under one directory.

    Writes are incremental by design: `write_page` and `write_graph` are called
    as the crawl proceeds, so stopping the crawler early still leaves a
    complete, servable dataset for everything visited so far.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def create(self) -> None:
        """Create the output tree. Safe to call on an existing directory."""
        (self.directory / PAGES_DIR).mkdir(parents=True, exist_ok=True)
        (self.directory / SCREENSHOTS_DIR).mkdir(parents=True, exist_ok=True)

    def screenshot_path(self, node_id: str) -> Path:
        """Absolute path to write *node_id*'s screenshot to."""
        return self.directory / SCREENSHOTS_DIR / screenshot_filename(node_id)

    def screenshot_rel(self, node_id: str) -> str:
        """The screenshot path as stored in the data, relative to the output."""
        return f"{SCREENSHOTS_DIR}/{screenshot_filename(node_id)}"

    def write_page(self, record: dict) -> None:
        """Write one page record to ``pages/``."""
        node_id = record["id"]
        path = self.directory / PAGES_DIR / page_filename(node_id)
        _write_json(path, record)

    def write_graph(self, graph: Graph) -> None:
        """Write the whole graph to ``graph.json``.

        Called after every page, so it is always at most one page behind. The
        file is self-contained, so this alone is enough for the UI to draw.
        """
        _write_json(self.directory / GRAPH_FILE, graph.to_dict())
