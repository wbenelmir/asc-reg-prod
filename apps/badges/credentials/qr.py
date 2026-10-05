"""Server-side QR rendering for the Digital Entry Pass.

Phase 3 Prompt 2 correction pass. Before this module existed the participant
page showed the compact JWS as text and called it a QR code; it was not one,
and nothing at a gate could have scanned it.

Rendering happens here, in memory, using `segno` -- a pure-Python encoder
with no native build step, no transitive dependencies, and no network call.
No external QR service is contacted, and the token never becomes part of a
URL or query string: the image endpoint addresses a pass by its safe public
identifier and reconstructs the token server-side.

The rendered bytes are credential material. Callers must not log them, cache
them, or write them to disk.
"""

from __future__ import annotations

import io

#: Error-correction level M (~15% recovery). Chosen over L because an entry
#: pass is printed, folded, and read on a phone screen at a gate; chosen over
#: Q/H because a ~380-character payload at higher correction pushes the symbol
#: into versions that scan poorly on small displays.
ERROR_CORRECTION = "m"

#: The standard requires a 4-module quiet zone. Scanners fail intermittently
#: without it, which is precisely the kind of defect that only shows up on
#: event day, so it is set explicitly rather than left to a default.
QUIET_ZONE_MODULES = 4

#: Module size for the raster form. The pass payload encodes to QR version
#: 16 (81x81 modules), so 8 device pixels per module yields a ~712px image
#: that stays crisp when the page scales it down to its 320px display box.
PNG_SCALE = 8


class QrRenderError(RuntimeError):
    """Raised when a credential cannot be rendered. Never contains the token."""


def render_png(compact_jws: str) -> bytes:
    """Render `compact_jws` as a PNG QR symbol and return the bytes."""
    return _render(compact_jws, kind="png", scale=PNG_SCALE)


def render_svg(compact_jws: str) -> bytes:
    """Render `compact_jws` as an SVG QR symbol and return the bytes."""
    return _render(compact_jws, kind="svg", scale=4)


def _render(compact_jws: str, *, kind: str, scale: int) -> bytes:
    import segno

    if not isinstance(compact_jws, str) or not compact_jws:
        raise QrRenderError("A credential token is required to render a QR symbol.")
    try:
        symbol = segno.make(compact_jws, error=ERROR_CORRECTION)
    except Exception as exc:  # noqa: BLE001 - never echo the token
        raise QrRenderError("The credential could not be encoded as a QR symbol.") from exc

    buffer = io.BytesIO()
    symbol.save(buffer, kind=kind, scale=scale, border=QUIET_ZONE_MODULES)
    return buffer.getvalue()


def symbol_version(compact_jws: str) -> int:
    """The QR version (symbol size) a credential encodes to. Diagnostics only."""
    import segno

    return segno.make(compact_jws, error=ERROR_CORRECTION).version
