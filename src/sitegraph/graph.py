"""The crawl graph: nodes are normalized URLs, edges are directed links.

This module is deliberately free of any browser or filesystem dependency, so
the graph rules can be tested directly. `store.py` writes what this produces;
`crawl.py` feeds it.

Two decisions worth stating, because both are places a naive implementation
quietly gets the output wrong:

- **Edges are derived from the nodes' links at serialization time**, not
  appended as pages are visited. A page's links are known the moment it is
  visited, but its *targets* may not have been visited yet (they are only
  discovered later in the breadth-first walk). Deriving on read means an edge
  can never reference a node that is absent from ``nodes``, and there is no
  second code path that could drift from the first.
- **A page's ``links`` record every internal target it points at**, including
  targets that were never visited (cut off by ``--max-pages``). Those targets
  are visible in the page record and the inspector, but they produce no edge,
  because an edge to a page that was never captured would be a lie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sitegraph.urls import normalize_url

__all__ = ["Graph", "Node"]

#: IDs are zero-padded strings ("000012"), never integers — spec §6 and the
#: edge/root references depend on that. Six digits holds a million pages, far
#: past anything this crawler should be pointed at.
ID_WIDTH = 6


def format_id(number: int) -> str:
    """Return the canonical string ID for a 1-based *number*."""
    return str(number).zfill(ID_WIDTH)


@dataclass(slots=True)
class Node:
    """One crawled page.

    ``screenshot`` is a path *relative to the output directory*
    (``screenshots/000012.webp``), never absolute, so that a `serve` run rooted
    elsewhere still resolves it. It is ``None`` for a page that failed before it
    could be captured.
    """

    id: str
    url: str
    title: str
    depth: int
    status: int | None
    screenshot: str | None
    failed: bool = False
    error: str | None = None
    links: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        """The URL's path, with query, for display in the UI."""
        parts = urlsplit(self.url)
        return parts.path + (f"?{parts.query}" if parts.query else "")


class Graph:
    """A directed graph of crawled pages, keyed by normalized URL.

    Nodes are added in visit order, which is also ID order, so ``nodes`` is
    already sorted and serialization needs no separate sort step.
    """

    def __init__(self) -> None:
        self._by_url: dict[str, Node] = {}
        self._nodes: list[Node] = []
        self.root_id: str | None = None

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def nodes(self) -> list[Node]:
        """Nodes in ID order. The list is live — treat it as read-only."""
        return self._nodes

    def node_id(self, url: str) -> str | None:
        """Return the ID of the node for *url*, or ``None`` if it has none.

        *url* is normalized first, so a caller holding a raw href gets the same
        answer as one holding a canonical URL.
        """
        node = self._by_url.get(normalize_url(url))
        return node.id if node else None

    def get(self, node_id: str) -> Node | None:
        """Return the node with *node_id*, or ``None``."""
        # IDs are dense and 1-based, so this is a valid index rather than a
        # dict lookup — and it cannot accept "000012" as an int by accident.
        if not node_id.isdigit():
            return None
        index = int(node_id) - 1
        if 0 <= index < len(self._nodes):
            node = self._nodes[index]
            return node if node.id == node_id else None
        return None

    def add_page(
        self,
        url: str,
        *,
        title: str = "",
        depth: int = 0,
        status: int | None = None,
        screenshot: str | None = None,
        failed: bool = False,
        error: str | None = None,
        links: list[str] | None = None,
        root: bool = False,
    ) -> Node:
        """Add (or update) the page at *url* and return its node.

        Adding the same URL twice updates the existing node rather than
        creating a duplicate. That is what makes node identity a normalized
        URL rather than a visit count.

        Passing ``root=True`` marks the node as the crawl's origin. The crawler
        sets it on the page it is about to visit rather than up front, so that
        ``root`` in the payload always names a node that is actually present.
        """
        canonical = normalize_url(url)
        existing = self._by_url.get(canonical)

        if existing is not None:
            existing.title = title or existing.title
            existing.status = status if status is not None else existing.status
            existing.screenshot = screenshot or existing.screenshot
            existing.failed = failed
            existing.error = error
            if links is not None:
                existing.links = links
            if root:
                self.root_id = existing.id
            return existing

        node = Node(
            id=format_id(len(self._nodes) + 1),
            url=canonical,
            title=title,
            depth=depth,
            status=status,
            screenshot=screenshot,
            failed=failed,
            error=error,
            links=links or [],
        )
        self._by_url[canonical] = node
        self._nodes.append(node)
        if root:
            self.root_id = node.id
        return node

    def edges(self) -> list[tuple[str, str]]:
        """Return the deduplicated directed edges, sorted for stable output.

        One edge per ordered pair, however many links point from A to B, and
        self-links are structurally impossible because the target must be a
        *different* node to have an ID at all.
        """
        pairs = {
            (node.id, target.id)
            for node in self._nodes
            for target in map(self._by_url.get, node.links)
            if target is not None and target is not node
        }
        return sorted(pairs)

    def to_dict(self) -> dict:
        """Return the ``graph.json`` payload (spec §6).

        Self-contained and independent of the per-page files — everything the
        graph view needs to draw is here.
        """
        return {
            "root": self.root_id,
            "nodes": [
                {
                    "id": node.id,
                    "url": node.url,
                    "title": node.title,
                    "depth": node.depth,
                    "status": node.status,
                    "failed": node.failed,
                    "screenshot": node.screenshot,
                }
                for node in self._nodes
            ],
            "edges": [
                {"source": source, "target": target}
                for source, target in self.edges()
            ],
        }

    def page_dict(self, node: Node) -> dict:
        """Return the per-page record for *node* (spec §5).

        ``links`` here is the full set of internal targets, not just the ones
        that became nodes — that difference is the point of keeping the page
        record separate from the graph.
        """
        return {
            "id": node.id,
            "url": node.url,
            "title": node.title,
            "status": node.status,
            "depth": node.depth,
            "screenshot": node.screenshot,
            "failed": node.failed,
            "error": node.error,
            "links": list(node.links),
        }