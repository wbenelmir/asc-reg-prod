"""Derive the compact ASC mark (favicon and app icon) from the authorized logo.

Deterministic asset tooling for UX-1 (decision D-04). The mark is the
graphic part of `static/img/brand/asc-logo.svg` (the twelve stripe paths that
draw the map of Africa), without the wordmark. The script copies those path
elements byte for byte, so the mark inherits the logo's exact geometry and
colours; nothing is redrawn or recoloured.

Outputs, all under `static/img/brand/`:
  asc-mark.svg       square SVG, transparent background (browser tab icon)
  asc-mark-32.png    32 x 32, transparent (fallback tab icon, /favicon.ico)
  asc-mark-180.png   180 x 180, white background (touch icon)

The SVG is a pure function of the logo. The PNGs are rendered with the
Chromium that Playwright manages for the browser test suite; the script
prints the browser version and the SHA-256 of every file so a rebuild can be
compared. Run it from the project root:

    uv run python scripts/build_brand_icons.py

It reads and writes only the three brand files and touches nothing else.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BRAND_DIR = BASE_DIR / "static" / "img" / "brand"
LOGO = BRAND_DIR / "asc-logo.svg"

# The graphic occupies logo paths 24..35 (0-based) and spans x 317..536, y 0..239.
# A square 250-unit window centred on that box keeps a small margin.
GRAPHIC_PATH_START = 24
VIEW_BOX = "302 -5 250 250"


def _logo_paths() -> list[str]:
    text = LOGO.read_text(encoding="utf-8")
    paths = re.findall(r"<path\b[^>]*/>", text)
    if len(paths) != 36:
        raise SystemExit(f"unexpected logo structure: {len(paths)} paths (expected 36)")
    return paths[GRAPHIC_PATH_START:]


def build_svg() -> str:
    body = "\n".join(_logo_paths())
    return (
        f'<svg width="250" height="250" viewBox="{VIEW_BOX}" fill="none" '
        'xmlns="http://www.w3.org/2000/svg">\n'
        f"{body}\n"
        "</svg>\n"
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    from playwright.sync_api import sync_playwright

    svg_text = build_svg()
    svg_path = BRAND_DIR / "asc-mark.svg"
    svg_path.write_text(svg_text, encoding="utf-8", newline="\n")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        print(f"chromium {browser.version}")
        for size, background, padding, name in (
            (32, None, 0, "asc-mark-32.png"),
            (180, "#ffffff", 20, "asc-mark-180.png"),
        ):
            page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
            inner = size - 2 * padding
            page.set_content(
                "<!doctype html><html><body style='margin:0;"
                f"background:{background or 'transparent'};'>"
                f"<div style='width:{size}px;height:{size}px;padding:{padding}px;"
                "box-sizing:border-box'>"
                f"<div style='width:{inner}px;height:{inner}px'>"
                + svg_text.replace(
                    '<svg width="250" height="250"',
                    f'<svg width="{inner}" height="{inner}"',
                )
                + "</div></div></body></html>"
            )
            page.screenshot(
                path=str(BRAND_DIR / name),
                omit_background=background is None,
                clip={"x": 0, "y": 0, "width": size, "height": size},
            )
            page.close()
        browser.close()

    for name in ("asc-mark.svg", "asc-mark-32.png", "asc-mark-180.png"):
        print(f"{_digest(BRAND_DIR / name)}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
