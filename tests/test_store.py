"""Tests for the on-disk layout.

The layout is a public interface (the server and the UI read it), so the file
names and the relative paths inside the data are pinned here rather than left
to whatever the writer happens to produce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sitegraph.graph import Graph
from sitegraph.store import (
    GRAPH_FILE,
    PAGES_DIR,
    SCREENSHOTS_DIR,
    THUMBS_DIR,
    CrawlNotFound,
    CrawlStore,
    load_graph,
    page_filename,
    screenshot_filename,
    thumbnail_filename,
)


def test_store_creates_the_documented_tree(tmp_path: Path) -> None:
    store = CrawlStore(tmp_path / ".sitegraph")
    store.create()
    graph = Graph()
    graph.add_page("http://example.com/", root=True)
    store.write_page(graph.page_dict(graph.nodes[0]))
    store.write_graph(graph)

    assert (tmp_path / ".sitegraph" / GRAPH_FILE).is_file()
    assert (tmp_path / ".sitegraph" / PAGES_DIR / "000001.json").is_file()
    assert (tmp_path / ".sitegraph" / SCREENSHOTS_DIR).is_dir()
    assert (tmp_path / ".sitegraph" / THUMBS_DIR).is_dir()


def test_create_is_idempotent(tmp_path: Path) -> None:
    store = CrawlStore(tmp_path / "out")
    store.create()
    store.create()

    assert (tmp_path / "out" / PAGES_DIR).is_dir()


def test_filenames_are_zero_padded(tmp_path: Path) -> None:
    assert page_filename("000012") == "000012.json"
    assert screenshot_filename("000012") == "000012.webp"
    assert thumbnail_filename("000012") == "000012.webp"


def test_screenshot_paths_are_relative_to_the_output_directory() -> None:
    """Absolute paths would break a `serve` run rooted elsewhere."""
    store = CrawlStore(Path("/tmp/somewhere"))

    assert store.screenshot_rel("000007") == "screenshots/000007.webp"
    assert not store.screenshot_rel("000007").startswith("/")
    assert store.screenshot_path("000007") == Path(
        "/tmp/somewhere/screenshots/000007.webp"
    )
    assert store.thumbnail_rel("000007") == "thumbs/000007.webp"
    assert store.thumbnail_path("000007") == Path("/tmp/somewhere/thumbs/000007.webp")


def test_the_scratch_directory_is_swept_on_create(tmp_path: Path) -> None:
    """Anything left there belongs to a run that never committed it."""
    store = CrawlStore(tmp_path / "out")
    store.create()
    orphan = store.incoming_path(1)
    orphan.write_bytes(b"left over from a run that died")

    store.create()

    assert not orphan.exists()
    assert orphan.parent.is_dir(), "the directory itself is recreated"


def test_a_capture_is_moved_into_place_under_the_node_id(tmp_path: Path) -> None:
    store = CrawlStore(tmp_path / "out")
    store.create()
    scratch = store.incoming_path(7)
    scratch.write_bytes(b"a picture")

    assert store.adopt_screenshot(scratch, "000012") is True
    assert not scratch.exists()
    assert store.screenshot_path("000012").read_bytes() == b"a picture"
    assert store.screenshot_rel("000012") == "screenshots/000012.webp"


def test_a_capture_that_never_landed_is_reported(tmp_path: Path) -> None:
    """A page can render and still have no picture; saying so beats writing a
    `screenshot` path that points at nothing."""
    store = CrawlStore(tmp_path / "out")
    store.create()

    assert store.adopt_screenshot(store.incoming_path(1), "000012") is False


def test_a_thumbnail_is_written_where_the_graph_says_it_is(tmp_path: Path) -> None:
    """The bytes come from the renderer rather than from a scratch file, so this
    is the one image write with no `os.replace` to inherit — it has to land
    under the name `thumbnail_rel` promises."""
    store = CrawlStore(tmp_path / "out")
    store.create()

    store.write_thumbnail("000012", b"a small picture")

    assert store.thumbnail_path("000012").read_bytes() == b"a small picture"
    assert (tmp_path / "out" / store.thumbnail_rel("000012")).is_file()


def test_a_thumbnail_write_never_leaves_a_half_file(tmp_path: Path) -> None:
    """A killed crawl must not leave a truncated image for the graph to point at."""
    store = CrawlStore(tmp_path / "out")
    store.create()

    store.write_thumbnail("000012", b"first")

    assert not list((tmp_path / "out" / THUMBS_DIR).glob("*.tmp"))
    assert store.thumbnail_path("000012").read_bytes() == b"first"


def test_thumbnails_are_kept_out_of_the_screenshots_directory(tmp_path: Path) -> None:
    """`screenshots/` is exactly one file per rendered page — a property the
    tests and `--resume` both count on — so the copies live beside it."""
    store = CrawlStore(tmp_path / "out")
    store.create()
    store.write_thumbnail("000012", b"a small picture")

    assert list((tmp_path / "out" / SCREENSHOTS_DIR).glob("*.webp")) == []


def test_json_is_written_atomically(tmp_path: Path) -> None:
    """A killed crawl must never leave a half-written graph.json behind, so the
    write lands through a temporary file that is renamed into place."""
    store = CrawlStore(tmp_path)
    store.create()
    graph = Graph()
    graph.add_page("http://example.com/", root=True)
    store.write_graph(graph)

    assert not list(tmp_path.rglob("*.tmp")), "temporary file left behind"


def test_write_graph_round_trips(tmp_path: Path) -> None:
    store = CrawlStore(tmp_path / "out")
    store.create()
    graph = Graph()
    graph.add_page("http://example.com/", root=True)
    graph.add_page("http://example.com/about", links=["http://example.com/"])
    store.write_graph(graph)

    assert load_graph(tmp_path / "out") == graph.to_dict()


def test_written_json_is_readable_text(tmp_path: Path) -> None:
    """The output is meant to be inspected by hand."""
    store = CrawlStore(tmp_path / "out")
    store.create()
    graph = Graph()
    graph.add_page("http://example.com/", title="Übersicht — 概要", root=True)
    store.write_graph(graph)

    # Indented, newline-terminated, and not one long line or a wall of \uXXXX
    # escapes: `ensure_ascii=False` keeps titles legible in the file.
    text = (tmp_path / "out" / GRAPH_FILE).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "\n  " in text
    assert "Übersicht — 概要" in text
    assert json.loads(text)["root"] == "000001"


def test_load_graph_reports_a_missing_crawl(tmp_path: Path) -> None:
    with pytest.raises(CrawlNotFound) as excinfo:
        load_graph(tmp_path / "nowhere")

    assert "nowhere" in str(excinfo.value)


def test_load_graph_reports_a_corrupt_file(tmp_path: Path) -> None:
    (tmp_path / GRAPH_FILE).write_text("{not json", encoding="utf-8")

    with pytest.raises(CrawlNotFound):
        load_graph(tmp_path)


def test_missing_file_after_create_is_still_loadable_error(tmp_path: Path) -> None:
    """`serve` on a directory that exists but was never crawled."""
    store = CrawlStore(tmp_path / "out")
    store.create()

    with pytest.raises(CrawlNotFound):
        load_graph(tmp_path / "out")
