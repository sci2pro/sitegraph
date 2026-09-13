"""Command-line surface for sitegraph."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sitegraph import __version__

DEFAULT_OUTPUT = ".sitegraph"
DEFAULT_PORT = 4777
DEFAULT_MAX_PAGES = 500

#: Inside the output directory on purpose: `.sitegraph/` is already gitignored,
#: and `serve` does not route it, so a file full of session cookies lands
#: somewhere that is neither committed nor downloadable.
DEFAULT_SESSION = ".sitegraph/session.json"


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
    crawl.add_argument(
        "--storage-state",
        metavar="FILE",
        default=None,
        help="crawl as a signed-in user, using a session saved by `sitegraph login`",
    )
    crawl.add_argument(
        "--resume",
        action="store_true",
        help=(
            "continue a crawl in --output that stopped early, instead of "
            "starting over; --max-pages then caps this run rather than the crawl"
        ),
    )

    login = sub.add_parser(
        "login", help="Sign in once and save the session for `crawl`"
    )
    login.add_argument("url", help="page to sign in at, e.g. http://localhost:3000/login")
    login.add_argument(
        "--storage-state",
        metavar="FILE",
        default=DEFAULT_SESSION,
        help=f"where to save the session (default: {DEFAULT_SESSION})",
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
        from sitegraph.crawl import ResumeError, crawl
        from sitegraph.urls import InvalidURL

        storage_state = Path(args.storage_state) if args.storage_state else None
        if storage_state is not None and not storage_state.is_file():
            # Caught here rather than inside the browser: Playwright's own
            # error for a missing state file does not say how to make one.
            raise SystemExit(
                f"error: no session at {storage_state}\n"
                f"       run `sitegraph login {args.url}` first, or drop "
                f"--storage-state to crawl signed out."
            )

        try:
            crawl(
                url=args.url,
                output=Path(args.output),
                max_pages=args.max_pages,
                resume=args.resume,
                storage_state=storage_state,
            )
        except ResumeError as exc:
            raise SystemExit(
                f"error: cannot resume: {exc}\n"
                f"       drop --resume to start a fresh crawl."
            ) from None
        except InvalidURL as exc:
            # The common case is a bare "localhost:3000", which urlsplit reads
            # as scheme "localhost" — worth naming explicitly.
            raise SystemExit(
                f"error: {exc}\n"
                f"       expected an absolute http(s) URL, "
                f'e.g. "http://localhost:3000".'
            ) from None
    elif args.command == "login":
        from sitegraph.login import describe, login
        from sitegraph.urls import InvalidURL

        try:
            session = login(url=args.url, storage_state=Path(args.storage_state))
        except InvalidURL as exc:
            raise SystemExit(
                f"error: {exc}\n"
                f"       expected an absolute http(s) URL, "
                f'e.g. "http://localhost:3000/login".'
            ) from None

        print(describe(session))
        if session.cookies == 0:
            print("  (a session with no cookies may not be a signed-in one)")
    elif args.command == "serve":
        from sitegraph.serve import serve

        serve(directory=Path(args.dir), port=args.port)

    return 0