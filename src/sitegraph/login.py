"""Sign in to a site in a real browser, and keep the session for `crawl`.

Deliberately **not** a login automation: `login` opens a visible Chromium and
waits for a human to authenticate — including SSO, MFA, and anything else that
cannot be scripted. sitegraph never sees a credential and never drives a form;
all it does is save the session that resulted (spec §11 keeps login automation
out of scope, and this stays on the right side of that line).

The saved file is Playwright's storage state: cookies plus ``localStorage`` per
origin. `crawl --storage-state` hands it straight back to the browser, so a
crawl can see the parts of an app that sit behind a sign-in.

Treat the file as a credential. It contains live session cookies, which is
enough for anyone holding it to act as the signed-in user.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from sitegraph.urls import normalize_url

__all__ = ["Session", "login"]

#: How often the wait loop checks whether the browser window has gone away.
_POLL_SECONDS = 0.2


@dataclass(frozen=True, slots=True)
class Session:
    """What a `login` run produced, for reporting and for tests."""

    url: str
    storage_state: Path
    cookies: int
    #: Where the start URL ended up when the saved session was replayed, and
    #: what came back. A browser that lands somewhere unexpected (a login page,
    #: usually) means the session did not take.
    final_url: str | None = None
    status: int | None = None
    title: str = ""


def _wait_for_sign_in(page) -> None:
    """Block until the human says they are done.

    Enter on stdin is the documented signal, but closing the browser window is
    the other obvious way to finish — so the wait ends on either, rather than
    leaving a terminal stuck on a prompt for a window that no longer exists.
    """
    done = threading.Event()

    def listen() -> None:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            # EOF means stdin is not a terminal (piped or closed); treat it as
            # "done", and let the verification below report whether that was
            # enough. Ctrl-C arrives here as KeyboardInterrupt.
            pass
        finally:
            done.set()

    # The reader only touches stdin; every Playwright call stays on the main
    # thread, because the sync API is not safe to use from another one.
    threading.Thread(target=listen, daemon=True).start()

    browser = page.context.browser
    while not done.wait(_POLL_SECONDS):
        if browser is None or not browser.is_connected():
            return


def _count_cookies(path: Path) -> int:
    """Read the saved file back, so a session we claim to have written is one
    that actually parses."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(state.get("cookies") or [])


def login(
    url: str,
    storage_state: Path,
    *,
    headless: bool = False,
    confirm=None,
) -> Session:
    """Open *url*, let the user sign in, and write the session to *storage_state*.

    ``confirm`` is called once the page has loaded and is expected to return
    when the user is finished; it defaults to waiting for Enter (or for the
    window to be closed). Tests pass their own.
    """
    target = normalize_url(url)
    path = Path(storage_state)
    path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=headless)
        except PlaywrightError as exc:
            raise SystemExit(
                f"error: could not open a browser window: {exc}\n"
                f"       `login` needs a display — run it where you sign in."
            ) from None

        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(target)

            print(f"Sign in at {target} in the browser window that just opened.")
            print("Press Enter here when you are done (or close the window).")
            (confirm or _wait_for_sign_in)(page)

            if browser.is_connected():
                context.storage_state(path=str(path))
            else:
                raise SystemExit(
                    "error: the browser was closed before a session could be saved"
                )

            session = _replay(browser, path, target)
        finally:
            if browser.is_connected():
                browser.close()

    return session


def _replay(browser, path: Path, target: str) -> Session:
    """Load the saved state into a fresh context and revisit *target*.

    This is the only way to know the file is worth keeping: pressing Enter
    before actually signing in is the easy mistake, and without this the crawl
    would quietly capture the login page under every URL it visited.
    """
    cookies = _count_cookies(path)
    context = browser.new_context(storage_state=str(path))
    try:
        page = context.new_page()
        response = page.goto(target)
        return Session(
            url=target,
            storage_state=path,
            cookies=cookies,
            final_url=page.url,
            status=response.status if response is not None else None,
            title=page.title(),
        )
    finally:
        context.close()


def describe(session: Session) -> str:
    """A short human-readable summary of a `login` run."""
    lines = [
        f"Saved {session.cookies} cookie(s) to {session.storage_state}",
        f"  replayed as {session.final_url} "
        f"({session.status}{f', {session.title!r}' if session.title else ''})",
    ]
    if session.cookies == 0:
        lines.append(
            "  no cookies were captured — if this app does not use them, the "
            "session may still be in localStorage, which is saved too."
        )
    return "\n".join(lines)
