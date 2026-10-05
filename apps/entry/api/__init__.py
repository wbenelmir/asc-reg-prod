"""Device API boundary, `/entry/api/v1/` (Phase 4 Prompt 2, binding decision P2-A).

Django REST Framework is used here and nowhere else. Every endpoint is
JSON-only, versioned, `Cache-Control: no-store`, CSRF-protected, and
documented in `docs/api/entry_device_api_v1.openapi.json` (contract-tested).

The namespace is `/entry/api/v1/` rather than TRD §15.1's example
`/api/v1/entry/` because the approved Phase 3 device credential cookie is
scoped to `Path=/entry/` (ADR-0021): widening that cookie to `/` would send
the device credential with every request to the public site. See ADR-0023.
"""
