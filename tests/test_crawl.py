"""Tests for the crawl walk itself.

These run against a `FakeRenderer` rather than Chromium: the ordering, depth,
deduplication, origin restriction and persistence rules are the crawler's
logic, and testing them through a browser would make them slow, flaky, and
hard to aim at a specific case. The real browser is covered separately by
`test_e2e.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sitegraph.crawl import PageResult, crawl
from sitegraph.store import GRAPH_FILE, PAGES_DIR, SCREENSHOTS_DIR

ROOT = "http://example.com/"

WEBP_MAGIC = b"RIFF\x00\x00\x00\x00WEBP"


class FakeRenderer:
    """Serves a fixed map of URL -> PageResult, writing real screenshot files.

    ``on_visit`` runs at the start of each visit, which is what lets a test
    observe the on-disk state mid-crawl.
    """

    def __init__(
        self,
        pages: dict[str, PageResult],
        *,
        on_visit=None,
        screenshots: bool = True,
    ) -> None:
        self.pages = pages
        self.on_visit = on_visit
        self.screenshots = screenshots
        self.visited: list[str] = []
        self.closed = False

    def __enter__(self) -> FakeRenderer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def visit(self, url: str, screenshot_path: Path) -> PageResult:
        self.visited.append(url)
        if self.on_visit is not None:
            self.on_visit(url, screenshot_path)
        result = self.pages.get(url)
        if result is None:
            return PageResult(failed=True, error="net::ERR_NAME_NOT_RESOLVED")
        if not result.failed and self.screenshots:
            screenshot_path.write_bytes(WEBP_MAGIC)
        return result


def result(*hrefs: str, title: str = "Page", status: int = 200) -> PageResult:
    return PageResult(status=status, title=title, hrefs=list(hrefs))


def read_graph(directory: Path) -> dict:
    return json.loads((directory / GRAPH_FILE).read_text(encoding="utf-8"))


def nodes_by_url(directory: Path) -> dict[str, dict]:
    return {node["url"]: node for node in read_graph(directory)["nodes"]}


# --- walk ---------------------------------------------------------------


def test_visits_each_page_once(tmp_path: Path) -> None:
    """A diamond — both B and C link to D — must still visit D once."""
    pages = {
        ROOT: result("/b", "/c"),
        "http://example.com/b": result("/d"),
        "http://example.com/c": result("/d"),
        "http://example.com/d": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert sorted(renderer.visited) == [
        "http://example.com/",
        "http://example.com/b",
        "http://example.com/c",
        "http://example.com/d",
    ]


def test_crawls_breadth_first(tmp_path: Path) -> None:
    """Order matters because it sets the IDs, and IDs are the graph's interface."""
    pages = {
        ROOT: result("/b", "/c"),
        "http://example.com/b": result("/d"),
        "http://example.com/c": result("/e"),
        "http://example.com/d": result(),
        "http://example.com/e": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.visited == [
        ROOT,
        "http://example.com/b",
        "http://example.com/c",
        "http://example.com/d",
        "http://example.com/e",
    ]


def test_depth_is_the_shortest_path_from_the_root(tmp_path: Path) -> None:
    """A page reachable at depth 1 and depth 3 is recorded at depth 1."""
    pages = {
        ROOT: result("/a", "/long"),
        "http://example.com/a": result("/b"),
        "http://example.com/b": result("/c"),
        "http://example.com/c": result("/deep"),
        "http://example.com/long": result(),
    }
    renderer = FakeRenderer(pages)
    # max_pages is high enough that /deep would be reached if it were enqueued
    # before /long; the depth recorded is what is being asserted.
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    by_url = nodes_by_url(tmp_path)
    assert by_url[ROOT]["depth"] == 0
    assert by_url["http://example.com/a"]["depth"] == 1
    assert by_url["http://example.com/b"]["depth"] == 2
    assert by_url["http://example.com/c"]["depth"] == 3


def test_max_pages_stops_the_walk(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/a", "/b", "/c"),
        "http://example.com/a": result(),
        "http://example.com/b": result(),
        "http://example.com/c": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 2, renderer=renderer)

    assert len(renderer.visited) == 2
    assert len(read_graph(tmp_path)["nodes"]) == 2


def test_pages_beyond_max_pages_remain_visible_as_links(tmp_path: Path) -> None:
    """Truncation must not hide what was discovered — the page record keeps the
    full link list even though only some targets became nodes."""
    pages = {
        ROOT: result("/a", "/b"),
        "http://example.com/a": result(),
    }
    crawl(ROOT, tmp_path, 1, renderer=FakeRenderer(pages))

    record = json.loads((tmp_path / PAGES_DIR / "000001.json").read_text())
    assert record["links"] == ["http://example.com/a", "http://example.com/b"]
    assert len(read_graph(tmp_path)["nodes"]) == 1


# --- origin restriction -------------------------------------------------


def test_stays_within_the_origin(tmp_path: Path) -> None:
    pages = {
        ROOT: result(
            "https://elsewhere.com/x",  # different host
            "/local",
            "http://example.com:8080/other-port",
            "https://example.com/scheme-differs",
            "mailto:someone@example.com",
        ),
        "http://example.com/local": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.visited == [ROOT, "http://example.com/local"]


# --- links and edges ----------------------------------------------------


def test_duplicate_links_produce_one_edge(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/a", "/a", "/a#fragment"),
        "http://example.com/a": result(),
    }
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    assert read_graph(tmp_path)["edges"] == [{"source": "000001", "target": "000002"}]


def test_self_links_are_dropped(tmp_path: Path) -> None:
    pages = {ROOT: result("/", "/#top", ROOT)}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    assert read_graph(tmp_path)["edges"] == []


def test_fragments_collapse_into_one_node(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/about#team", "/about#history", "/about"),
        "http://example.com/about": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.visited == [ROOT, "http://example.com/about"]
    assert len(read_graph(tmp_path)["nodes"]) == 2


def test_query_strings_stay_distinct(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/search?q=foo", "/search?q=bar"),
        "http://example.com/search?q=foo": result(),
        "http://example.com/search?q=bar": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert sorted(renderer.visited) == [
        ROOT,
        "http://example.com/search?q=bar",
        "http://example.com/search?q=foo",
    ]


# --- failure ------------------------------------------------------------


def test_failed_pages_stay_in_the_graph(tmp_path: Path) -> None:
    """Spec §7: a page that fails to render is still a node, marked failed."""
    pages = {
        ROOT: result("/broken"),
        # /broken is missing from the map, so the FakeRenderer fails it.
    }
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    by_url = nodes_by_url(tmp_path)
    broken = by_url["http://example.com/broken"]
    assert broken["failed"] is True
    assert broken["screenshot"] is None
    assert broken["status"] is None
    # and it is still a link target, so it is still an edge
    assert read_graph(tmp_path)["edges"] == [{"source": "000001", "target": "000002"}]


def test_failed_page_record_keeps_the_error(tmp_path: Path) -> None:
    pages = {ROOT: result("/broken")}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    record = json.loads((tmp_path / PAGES_DIR / "000002.json").read_text())
    assert record["error"] == "net::ERR_NAME_NOT_RESOLVED"
    assert record["failed"] is True


def test_http_error_status_is_not_a_render_failure(tmp_path: Path) -> None:
    """A 404 page renders perfectly well, and is worth seeing in the graph."""
    pages = {
        ROOT: result("/missing"),
        "http://example.com/missing": result(status=404, title="Not found"),
    }
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    missing = nodes_by_url(tmp_path)["http://example.com/missing"]
    assert missing["status"] == 404
    assert missing["failed"] is False
    assert missing["screenshot"] == "screenshots/000002.webp"


def test_crawl_continues_after_a_failure(tmp_path: Path) -> None:
    pages = {
        ROOT: result("/broken", "/fine"),
        "http://example.com/fine": result(),
    }
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.visited == [
        ROOT,
        "http://example.com/broken",
        "http://example.com/fine",
    ]
    assert len(read_graph(tmp_path)["nodes"]) == 3


# --- persistence --------------------------------------------------------


def test_writes_every_artifact(tmp_path: Path) -> None:
    pages = {ROOT: result("/a"), "http://example.com/a": result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    assert (tmp_path / GRAPH_FILE).is_file()
    assert (tmp_path / PAGES_DIR / "000001.json").is_file()
    assert (tmp_path / PAGES_DIR / "000002.json").is_file()
    assert (tmp_path / SCREENSHOTS_DIR / "000001.webp").read_bytes() == WEBP_MAGIC


def test_graph_is_written_before_the_first_page_is_visited(tmp_path: Path) -> None:
    """So an immediate interruption still leaves something servable."""
    seen: list[bool] = []

    def on_visit(url: str, path: Path) -> None:
        seen.append((tmp_path / GRAPH_FILE).is_file())

    pages = {ROOT: result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages, on_visit=on_visit))

    assert seen == [True]


def test_results_are_persisted_incrementally(tmp_path: Path) -> None:
    """The graph on disk must gain a node per visit, not appear at the end."""
    counts: list[int] = []

    def on_visit(url: str, path: Path) -> None:
        if (tmp_path / GRAPH_FILE).is_file():
            counts.append(len(read_graph(tmp_path)["nodes"]))

    pages = {
        ROOT: result("/a", "/b"),
        "http://example.com/a": result("/c"),
        "http://example.com/b": result(),
        "http://example.com/c": result(),
    }
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages, on_visit=on_visit))

    # Nothing is published until it has been captured, so the graph gains
    # exactly one node per completed visit.
    assert counts == [0, 1, 2, 3]


def test_graph_never_references_a_page_file_that_is_not_written(tmp_path: Path) -> None:
    def on_visit(url: str, path: Path) -> None:
        if not (tmp_path / GRAPH_FILE).is_file():
            return
        for node in read_graph(tmp_path)["nodes"]:
            assert (tmp_path / PAGES_DIR / f"{node['id']}.json").is_file()

    pages = {ROOT: result("/a"), "http://example.com/a": result("/b"),
             "http://example.com/b": result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages, on_visit=on_visit))


def test_interrupted_crawl_leaves_usable_data(tmp_path: Path) -> None:
    """Ctrl-C mid-crawl: what was finished is complete and parseable."""
    started: list[str] = []

    def on_visit(url: str, path: Path) -> None:
        started.append(url)
        if len(started) == 3:  # during the third visit, before it completes
            raise KeyboardInterrupt

    pages = {
        ROOT: result("/a", "/b", "/c"),
        "http://example.com/a": result(),
        "http://example.com/b": result(),
        "http://example.com/c": result(),
    }
    renderer = FakeRenderer(pages, on_visit=on_visit)

    with pytest.raises(SystemExit) as excinfo:
        crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert excinfo.value.code == 130
    payload = read_graph(tmp_path)  # must still parse
    assert [node["url"] for node in payload["nodes"]] == [
        ROOT,
        "http://example.com/a",
    ], "the interrupted page must not appear as if it had been captured"
    for node in payload["nodes"]:
        assert (tmp_path / PAGES_DIR / f"{node['id']}.json").is_file()
    assert renderer.closed, "the browser must be shut down on the way out"


def test_renderer_is_closed_on_a_normal_finish(tmp_path: Path) -> None:
    pages = {ROOT: result()}
    renderer = FakeRenderer(pages)
    crawl(ROOT, tmp_path, 100, renderer=renderer)

    assert renderer.closed


def test_screenshot_paths_in_the_graph_are_relative(tmp_path: Path) -> None:
    pages = {ROOT: result("/a"), "http://example.com/a": result()}
    crawl(ROOT, tmp_path, 100, renderer=FakeRenderer(pages))

    for node in read_graph(tmp_path)["nodes"]:
        assert node["screenshot"] == f"screenshots/{node['id']}.webp"
        assert not node["screenshot"].startswith("/")


def test_output_directory_is_created(tmp_path: Path) -> None:
    out = tmp_path / "nested" / ".sitegraph"
    crawl(ROOT, out, 100, renderer=FakeRenderer({ROOT: result()}))

    assert (out / GRAPH_FILE).is_file()


# --- input validation ---------------------------------------------------


@pytest.mark.parametrize("url", ["not a url", "localhost:3000", "ftp://example.com"])
def test_unusable_start_url_is_rejected(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError):
        crawl(url, tmp_path, 10, renderer=FakeRenderer({}))
