"""Canonical URL identity for sitegraph.

A node in the graph is a normalized URL. This module is the single owner of
that normalization — a second normalizer anywhere else is the one bug that
silently corrupts the graph, so every other module imports from here.

The rules (spec §4):

- Fragments are discarded: ``/about#team`` and ``/about#history`` are one node.
- Query strings are significant and their parameter *order* is preserved, so
  ``?b=2&a=1`` and ``?a=1&b=2`` stay distinct.
- Host and scheme are case-folded; path and query are **not** (paths are
  case-sensitive on essentially every server, so folding them would merge
  genuinely different pages).
- Default ports are dropped; every other port is kept.

Known limitation: because fragments are discarded, a hash-routed SPA
(``#/users/1``) collapses to a single node. Spec §11 puts "SPA states without
distinct URLs" out of scope, so this is intended, but it is the first thing a
user is likely to hit and think is a crawler bug.

Stdlib only — this must not import playwright, so that both the crawler and
the server can depend on it cheaply.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit

__all__ = [
    "InvalidURL",
    "Origin",
    "internal_links",
    "is_loopback",
    "is_internal",
    "normalize_url",
    "origin_of",
    "resolve_href",
]


class InvalidURL(ValueError):
    """Raised when a string is not an http(s) URL that can be normalized."""


#: Ports that are implied by the scheme and therefore dropped from the
#: canonical string. Note this is keyed by scheme: ``:443`` on an ``http`` URL
#: is *not* a default port and must be preserved.
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Characters that may appear literally in a path (or query) after
# normalization. Everything else is percent-encoded. ``#`` is absent from both
# sets on purpose, so no input can smuggle in a fragment or query delimiter.
# ``%`` is absent too: valid escapes are resolved before this check and invalid
# ones are encoded, so it must never reach the pass-through branch.
_PATH_SAFE = "/:@!$&'()*+,;=-._~[]"
_QUERY_SAFE = ":@!$&'()*+,;=-._~[]/?=&+"

# RFC 3986 unreserved characters, *minus* "." — see `_normalize_percent`.
_UNRESERVED_NO_DOT = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_~"
)

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _normalize_percent(text: str, safe: str) -> str:
    """Return *text* with percent-escapes normalized and raw characters encoded.

    Two touches are applied, and nothing else:

    - The hex digits of every valid ``%XX`` escape are uppercased, and escapes
      of unreserved characters are decoded (``%7E`` becomes ``~``).
    - Characters outside *safe* that are not part of an escape are encoded.

    ``.`` is deliberately excluded from the decoded set. ``%2E`` must never
    become ``.`` because ``/a/%2E%2E/b`` is a genuinely different resource from
    ``/a/../b`` — that difference is what stops path traversal, and decoding it
    would also make the browser re-interpret a path we had already resolved.
    Likewise ``%2F`` stays encoded so it is never confused with a separator.

    A ``%`` that does not begin a valid escape is encoded as ``%25`` rather than
    passed through, because passing it through breaks idempotence: ``%a%41``
    would emit ``%aA``, whose ``aA`` reads as a valid escape on the next pass and
    becomes ``%AA``. Non-idempotence is not cosmetic here — `origin_of` and
    `is_internal` re-normalize, so it could yield a different origin for a URL
    than the crawler computed for the same string.
    """
    out: list[str] = []
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if char == "%":
            if (
                index + 2 < length
                and text[index + 1] in _HEX_DIGITS
                and text[index + 2] in _HEX_DIGITS
            ):
                escape = text[index + 1 : index + 3]
                decoded = chr(int(escape, 16))
                if decoded in _UNRESERVED_NO_DOT:
                    out.append(decoded)
                else:
                    out.append(f"%{escape.upper()}")
                index += 3
            else:
                out.append("%25")
                index += 1
        elif char in safe:
            out.append(char)
            index += 1
        else:
            out.append(quote(char, safe=""))
            index += 1

    return "".join(out)


def _remove_dot_segments(path: str) -> str:
    """Remove ``.`` and ``..`` segments per RFC 3986 §5.2.4.

    Hand-written rather than delegated to ``posixpath.normpath``, which is
    wrong here in both directions: it collapses interior ``//`` (which must be
    preserved — ``/a//b`` is a different request target from ``/a/b``) while
    preserving a *leading* ``//``.

    Empty segments are kept, ``..`` never escapes the root, and a trailing
    ``.`` or ``..`` leaves a trailing slash behind (``/a/b/..`` is ``/a/``, not
    ``/a``).
    """
    if not path:
        return path

    segments = path.split("/")
    last_index = len(segments) - 1
    out: list[str] = []

    for index, segment in enumerate(segments):
        if segment == "." or segment == "..":
            if segment == "..":
                # Never pop past the root: an absolute path's leading "" is
                # structural and has to survive.
                floor = 1 if out and out[0] == "" else 0
                if len(out) > floor:
                    out.pop()
            if index == last_index:
                out.append("")
            continue
        out.append(segment)

    return "/".join(out)


def _split(url: str):
    """Parse *url*, raising `InvalidURL` unless it is a well-formed http(s) URL."""
    if not isinstance(url, str):
        raise InvalidURL(f"not a URL: {url!r}")

    # urlsplit raises a bare ValueError (not InvalidURL) on a malformed IPv6
    # literal such as "http://[::1/", so callers relying on InvalidURL would
    # otherwise see an uncaught crash on a single bad href.
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise InvalidURL(f"unparseable URL {url!r}: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise InvalidURL(f"unsupported scheme in {url!r}")

    # `.hostname` lowercases for us, and strips IPv6 brackets.
    if not parts.hostname:
        raise InvalidURL(f"missing host in {url!r}")

    try:
        parts.port  # noqa: B018 - property access validates the port
    except ValueError as exc:
        raise InvalidURL(f"invalid port in {url!r}: {exc}") from exc

    return parts


def _netloc(parts) -> str:
    """Rebuild the authority, folding host case and dropping default ports."""
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal — restore the brackets urlsplit removed
        host = f"[{host}]"

    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += f":{parts.password}"
        userinfo += "@"

    port = parts.port
    if port is not None and port != _DEFAULT_PORTS[parts.scheme.lower()]:
        return f"{userinfo}{host}:{port}"
    return f"{userinfo}{host}"


def normalize_url(url: str) -> str:
    """Return the canonical form of *url*, raising `InvalidURL` if unusable.

    Idempotent: ``normalize_url(normalize_url(u)) == normalize_url(u)``.
    """
    parts = _split(url)
    scheme = parts.scheme.lower()

    # Browsers treat a backslash in the path of an http(s) URL as a separator,
    # so leaving it alone would create a duplicate node for a resource the
    # crawler can never actually fetch as written. The query is left alone —
    # Chromium does not convert there either.
    path = _normalize_percent(parts.path.replace("\\", "/"), _PATH_SAFE)
    path = _remove_dot_segments(path) or "/"

    # A bare trailing "?" is indistinguishable from no query once parsed, and
    # carries no information, so it is dropped.
    query = _normalize_percent(parts.query, _QUERY_SAFE)

    normalized = f"{scheme}://{_netloc(parts)}{path}"
    if query:
        normalized += f"?{query}"
    return normalized


def resolve_href(href: str, base_url: str) -> str | None:
    """Resolve *href* against *base_url* and normalize it.

    Returns ``None`` — rather than raising — for anything that is not a usable
    http(s) target: empty hrefs, ``mailto:``/``javascript:``/``tel:``/``data:``,
    and malformed URLs. A crawler sees all of these constantly and none of them
    are errors.
    """
    if not href or not href.strip():
        return None
    try:
        return normalize_url(urljoin(base_url, href.strip()))
    except InvalidURL:
        return None


@dataclass(frozen=True, slots=True)
class Origin:
    """A scheme/host/port triple used for the same-origin restriction.

    ``port`` is ``None`` when it equals the scheme default, which is what makes
    the comparison a plain equality. Userinfo is deliberately excluded — two
    URLs differing only by credentials are the same origin.
    """

    scheme: str
    host: str
    port: int | None

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port is None:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"


def origin_of(url: str) -> Origin:
    """Return the origin of *url*, raising `InvalidURL` if it is not usable."""
    parts = urlsplit(normalize_url(url))
    return Origin(
        scheme=parts.scheme,
        host=parts.hostname or "",
        port=parts.port,
    )


def is_internal(url: str, origin: Origin) -> bool:
    """Return whether *url* shares *origin*. Unusable URLs are never internal."""
    try:
        return origin_of(url) == origin
    except InvalidURL:
        return False


def is_loopback(origin: Origin) -> bool:
    """Return whether *origin* is on this machine.

    Used to decide how hard the crawler may lean on a server by default: a
    localhost app is this tool's stated target, while anything else is someone
    else's machine and gets one connection unless the user asks for more.

    ``localhost`` is accepted by name as well as by address, since that is how
    it is usually written and it does not always resolve to a literal.
    """
    try:
        return ipaddress.ip_address(origin.host).is_loopback
    except ValueError:
        return origin.host == "localhost"


def internal_links(
    base_url: str, hrefs: Iterable[str], origin: Origin
) -> list[str]:
    """Return the crawlable targets among *hrefs*.

    Applies, in order: scheme filtering and normalization (via
    `resolve_href`), the same-origin restriction, self-link removal, and
    document-order deduplication. The result is exactly what belongs in a page
    record's ``links`` array and in the graph's edges — there is no second
    pipeline, so the two can never drift.
    """
    base = normalize_url(base_url)
    seen: set[str] = set()
    targets: list[str] = []

    for href in hrefs:
        target = resolve_href(href, base)
        if target is None or target == base:
            continue
        if not is_internal(target, origin):
            continue
        if target in seen:
            continue
        seen.add(target)
        targets.append(target)

    return targets
