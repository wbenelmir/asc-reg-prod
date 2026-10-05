"""Private-page protection: no indexing, no shared caching (UX-1, decision S-05).

Only one response is meant for search engines: the public registration start
page. Every other response, including sign-in, workspace, invitation, claim,
pass, export and entry pages, is marked `noindex, nofollow`, and each one that
does not already declare its own cache policy is `private, no-store`, so a
shared cache never keeps a token URL, a claim link or a pass.

The public start page is indexable but never shared-cacheable either (P4-4):
every response carries a per-visitor CSRF token and cookie, and a refused
submission re-renders the typed email address. It is therefore sent
`private, no-store` as well, with the robots meta tag as its only
search-engine signal.

The middleware only ever adds headers. A view that set its own
`Cache-Control` (for example the entry service worker, which must stay
revalidated rather than stored away) keeps it.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

#: The public, indexable start page for participants (`/` redirects to it).
PUBLIC_INDEXABLE_PATHS = frozenset({"/accounts/start/"})

#: Public text resources that carry no participant data. They keep normal
#: caching and are never marked private.
PUBLIC_RESOURCE_PATHS = frozenset({"/robots.txt", "/favicon.ico"})

NOINDEX_VALUE = "noindex, nofollow"
PRIVATE_CACHE_VALUE = "private, no-store"


def is_public_indexable(path: str) -> bool:
    return path in PUBLIC_INDEXABLE_PATHS


class PrivatePageProtectionMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        path = request.path
        if path in PUBLIC_RESOURCE_PATHS:
            return response
        if not is_public_indexable(path):
            response.headers["X-Robots-Tag"] = NOINDEX_VALUE
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = PRIVATE_CACHE_VALUE
        return response
