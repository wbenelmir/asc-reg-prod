"""Content Security Policy, Permissions-Policy and forwarded-scheme hygiene (P4-4).

`ContentSecurityPolicyMiddleware` sends one ENFORCED policy on every response
(plan §8, SEC-004). Every script, style, font, image, worker and manifest
comes from this origin:

* no inline script, no inline event handler and no inline `style` attribute
  exists in the project's templates (guarded by
  `apps/core/tests/test_p4_4_security_headers.py`), so the policy needs no
  `'unsafe-inline'`, no nonce and no hash;
* htmx 4 evaluates code only for `hx-on` and `js:` attributes, which the
  project does not use, so there is no `'unsafe-eval'`;
* the ALTCHA strict-CSP build loads its stylesheet and its PBKDF2 worker from
  this origin (`worker-src 'self'`), and the entry service worker and its
  manifest are same-origin too;
* `img-src data:` is needed only for the SVG data images inside the vendored
  Bootstrap stylesheet (select arrows, checkbox marks).

A view that sets its own `Content-Security-Policy` keeps it. The policy text
is built once from `settings.CONTENT_SECURITY_POLICY`.

`ForwardedProtoGuardMiddleware` runs first. Django's
`SECURE_PROXY_SSL_HEADER` trusts `X-Forwarded-Proto` from any client, so the
header is removed from every request whose direct peer is not inside
`TRUSTED_PROXY_CIDRS`. Only a configured trusted proxy can then tell Django
that the original request used HTTPS.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from functools import lru_cache

from django.conf import settings
from django.http import HttpRequest, HttpResponse

CSP_HEADER = "Content-Security-Policy"
PERMISSIONS_POLICY_HEADER = "Permissions-Policy"

#: The request META key of `X-Forwarded-Proto`.
FORWARDED_PROTO_META = "HTTP_X_FORWARDED_PROTO"


def build_policy(directives: dict[str, tuple[str, ...] | list[str]], *, upgrade: bool) -> str:
    """Serialize directives in a stable order. Raises on an empty source list,
    so a misconfiguration can never silently drop a directive."""
    parts = []
    for name, sources in directives.items():
        values = [str(value).strip() for value in sources if str(value).strip()]
        if not values:
            raise ValueError(f"CSP directive {name!r} has no source")
        parts.append(f"{name} {' '.join(values)}")
    if upgrade:
        parts.append("upgrade-insecure-requests")
    return "; ".join(parts)


@lru_cache(maxsize=8)
def _cached_policy(key: tuple) -> str:
    directives, upgrade = key
    return build_policy(dict(directives), upgrade=upgrade)


def current_policy() -> str:
    directives = getattr(settings, "CONTENT_SECURITY_POLICY", {})
    key = (
        tuple((name, tuple(values)) for name, values in directives.items()),
        bool(getattr(settings, "CONTENT_SECURITY_POLICY_UPGRADE_INSECURE_REQUESTS", False)),
    )
    return _cached_policy(key)


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if CSP_HEADER not in response.headers:
            response.headers[CSP_HEADER] = current_policy()
        permissions = getattr(settings, "PERMISSIONS_POLICY", "")
        if permissions and PERMISSIONS_POLICY_HEADER not in response.headers:
            response.headers[PERMISSIONS_POLICY_HEADER] = permissions
        return response


@lru_cache(maxsize=8)
def _networks(cidrs: tuple[str, ...]):
    return tuple(ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def peer_is_trusted_proxy(remote_addr: str) -> bool:
    if not getattr(settings, "TRUSTED_PROXY_FORWARDING_ENABLED", False):
        return False
    try:
        address = ipaddress.ip_address((remote_addr or "").strip())
    except ValueError:
        return False
    cidrs = tuple(getattr(settings, "TRUSTED_PROXY_CIDRS", ()) or ())
    return any(address in network for network in _networks(cidrs))


class ForwardedProtoGuardMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if FORWARDED_PROTO_META in request.META and not peer_is_trusted_proxy(
            request.META.get("REMOTE_ADDR", "")
        ):
            del request.META[FORWARDED_PROTO_META]
        return self.get_response(request)
