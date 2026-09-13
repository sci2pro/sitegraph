"""Tests for the graph model.

The rules being pinned here are the ones that silently corrupt output rather
than failing loudly: node identity, ID shape, and edge derivation.
"""

from __future__ import annotations

import pytest

from sitegraph.graph import Graph, format_id


def test_ids_are_zero_padded_strings() -> None:
    graph = Graph()
    node = graph.add_page("http://example.com/")

    assert node.id == "000001"
    assert isinstance(node.id, str)


def test_ids_count_up_in_visit_order() -> None:
    graph = Graph()
    graph.add_page("http://example.com/")
    graph.add_page("http://example.com/b")
    graph.add_page("http://example.com/c")

    assert [node.id for node in graph.nodes] == ["000001", "000002", "000003"]


def test_format_id_pads_to_six_digits() -> None:
    assert format_id(1) == "000001"
    assert format_id(12) == "000012"
    assert format_id(999999) == "999999"
    assert format_id(1000000) == "1000000"  # widens rather than truncating


def test_node_identity_is_the_normalized_url() -> None:
    """Fragments collapse, so two spellings are one node."""
    graph = Graph()
    first = graph.add_page("http://example.com/about")
    second = graph.add_page("http://example.com/about#team")

    assert first is second
    assert len(graph) == 1


def test_query_strings_are_distinct_nodes() -> None:
    graph = Graph()
    graph.add_page("http://example.com/search?q=foo")
    graph.add_page("http://example.com/search?q=bar")

    assert len(graph) == 2


def test_adding_a_page_twice_updates_rather_than_duplicates() -> None:
    graph = Graph()
    graph.add_page("http://example.com/", depth=0)
    node = graph.add_page("http://example.com/", title="Home", status=200, depth=0)

    assert len(graph) == 1
    assert node.id == "000001"
    assert node.title == "Home"
    assert node.status == 200


def test_root_flag_marks_the_root() -> None:
    graph = Graph()
    graph.add_page("http://example.com/", root=True)
    graph.add_page("http://example.com/about")

    assert graph.root_id == "000001"
    assert graph.to_dict()["root"] == "000001"


def test_root_flag_is_idempotent_on_update() -> None:
    """The crawler registers a node, then updates it with what the visit found."""
    graph = Graph()
    graph.add_page("http://example.com/", root=True)
    graph.add_page("http://example.com/", title="Home", status=200, root=True)

    assert graph.root_id == "000001"
    assert len(graph) == 1


def test_unknown_node_id_returns_none() -> None:
    graph = Graph()
    graph.add_page("http://example.com/")

    assert graph.get("000002") is None
    assert graph.get("nonsense") is None
    assert graph.get("000001") is not None


def test_edges_are_one_per_ordered_pair() -> None:
    """Many links from A to B still make a single edge (spec §6)."""
    graph = Graph()
    graph.add_page("http://example.com/", links=["http://example.com/b"] * 3)
    graph.add_page("http://example.com/b")

    assert graph.edges() == [("000001", "000002")]


def test_edges_are_directed() -> None:
    graph = Graph()
    graph.add_page("http://example.com/", links=["http://example.com/b"])
    graph.add_page("http://example.com/b", links=["http://example.com/"])

    assert graph.edges() == [("000001", "000002"), ("000002", "000001")]


def test_edges_to_unvisited_pages_are_omitted() -> None:
    """A link to a page that was never captured must not become an edge:
    graph.json would then reference a node it does not contain."""
    graph = Graph()
    graph.add_page("http://example.com/", links=["http://example.com/never-visited"])

    assert graph.edges() == []
    # ...but the link is still recorded on the page, which is the point of
    # keeping the per-page record separate from the graph.
    assert graph.page_dict(graph.nodes[0])["links"] == [
        "http://example.com/never-visited"
    ]


def test_self_links_are_not_edges() -> None:
    graph = Graph()
    graph.add_page("http://example.com/", links=["http://example.com/"])

    assert graph.edges() == []


def test_every_edge_endpoint_is_a_node() -> None:
    graph = Graph()
    graph.add_page("http://example.com/", links=["http://example.com/a"])
    graph.add_page("http://example.com/a", links=["http://example.com/b"])
    graph.add_page("http://example.com/b")

    payload = graph.to_dict()
    ids = {node["id"] for node in payload["nodes"]}
    for edge in payload["edges"]:
        assert edge["source"] in ids
        assert edge["target"] in ids


def test_node_ids_are_strings_in_the_payload() -> None:
    """Parsing "000012" as an int and re-serializing corrupts the graph."""
    graph = Graph()
    graph.add_page("http://example.com/", links=["http://example.com/b"])
    graph.add_page("http://example.com/b")
    payload = graph.to_dict()

    for node in payload["nodes"]:
        assert isinstance(node["id"], str)
    for edge in payload["edges"]:
        assert isinstance(edge["source"], str)
        assert isinstance(edge["target"], str)


def test_to_dict_is_self_contained() -> None:
    graph = Graph()
    graph.add_page("http://example.com/", root=True)
    node = graph.add_page(
        "http://example.com/about", title="About", depth=1, status=200
    )
    graph.nodes[0].links = [node.url]

    payload = graph.to_dict()
    assert payload["nodes"][1] == {
        "id": "000002",
        "url": "http://example.com/about",
        "title": "About",
        "depth": 1,
        "status": 200,
        "failed": False,
        "screenshot": None,
    }


def test_page_dict_carries_the_richer_record() -> None:
    graph = Graph()
    node = graph.add_page(
        "http://example.com/",
        title="Home",
        depth=0,
        status=200,
        screenshot="screenshots/000001.webp",
        links=["http://example.com/about"],
    )

    assert graph.page_dict(node) == {
        "id": "000001",
        "url": "http://example.com/",
        "title": "Home",
        "status": 200,
        "depth": 0,
        "screenshot": "screenshots/000001.webp",
        "failed": False,
        "error": None,
        "links": ["http://example.com/about"],
    }


def test_failed_pages_stay_in_the_graph() -> None:
    """Spec §7: a page that fails to render is still a node, marked failed."""
    graph = Graph()
    graph.add_page(
        "http://example.com/broken",
        failed=True,
        error="net::ERR_CONNECTION_REFUSED",
        screenshot=None,
    )

    payload = graph.to_dict()
    assert len(payload["nodes"]) == 1
    assert payload["nodes"][0]["failed"] is True
    assert payload["nodes"][0]["screenshot"] is None


def test_node_id_lookup_normalizes() -> None:
    graph = Graph()
    graph.add_page("http://example.com/about")

    assert graph.node_id("http://example.com/about#team") == "000001"
    assert graph.node_id("http://example.com/other") is None


def test_node_path_includes_the_query() -> None:
    graph = Graph()
    node = graph.add_page("http://example.com/search?q=foo")

    assert node.path == "/search?q=foo"


@pytest.mark.parametrize("url", ["not a url", "mailto:x@example.com", ""])
def test_invalid_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        Graph().add_page(url)
