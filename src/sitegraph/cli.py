"""Command-line surface for sitegraph."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sitegraph import __version__

DEFAULT_OUTPUT = ".sitegraph"
DEFAULT_PORT = 4777
DEFAULT_MAX_PAGES = 500


def build_parser() -> argparse.ArgumentParser:
    """Return the fully configured argument parser."""
    parser = argparse.ArgumentParser(
        prog="sitegraph",
        description="Crawl a site and explore its structure as a graph.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="Crawl a site and capture its pages")
    crawl.add_argument("url", help="start URL, e.g. http://localhost:3000")
    crawl.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"directory to write results to (default: {DEFAULT_OUTPUT})",
    )
    crawl.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"stop after this many pages (default: {DEFAULT_MAX_PAGES})",
    )

    serve = sub.add_parser("serve", help="Serve the visual explorer")
    serve.add_argument(
        "--dir",
        default=DEFAULT_OUTPUT,
        help=f"directory of crawl results (default: {DEFAULT_OUTPUT})",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to listen on (default: {DEFAULT_PORT})",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse *argv* and dispatch to the requested command."""
    args = build_parser().parse_args(argv)

    # Imported lazily: both commands pull in heavy dependencies (playwright)
    # that would otherwise slow down `--help` and shell completion.
    if args.command == "crawl":
        from sitegraph.crawl import crawl
        from sitegraph.urls import InvalidURL

        try:
            crawl(url=args.url, output=Path(args.output), max_pages=args.max_pages)
        except InvalidURL as exc:
            # The common case is a bare "localhost:3000", which urlsplit reads
            # as scheme "localhost" — worth naming explicitly.
            raise SystemExit(
                f"error: {exc}\n"
                f"       expected an absolute http(s) URL, "
                f'e.g. "http://localhost:3000".'
            ) from None
    elif args.command == "serve":
        from sitegraph.serve import serve

        serve(directory=Path(args.dir), port=args.port)

    return 0