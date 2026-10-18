#!/usr/bin/env python3
"""Build the Saku logo set as clean SVG from Yona's approved concept (ChatGPT sheet, 2026-10-18).

Symbol: three rounded blocks (many accounts) dropping into one green pocket (one picture).
Geometry is on a 100-unit grid; gaps between shapes are real transparency, not white strokes,
so the mark works on any background. The wordmark is Nunito 900 (SIL OFL) converted to paths.

Run: uv run --no-project --with fonttools python tools/build_logo.py
"""
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "saku/web/static/brand"
FONT = Path(__file__).resolve().parent / "fonts-src" / "nunito-900.ttf"  # static instance of Nunito[wght] at 900

C = {"green": "#0F6B4F", "mint": "#A7D7B5", "coral": "#E67D63", "cream": "#F0BE63", "ink": "#1F2D28"}

# --- symbol paths, 100x100 grid -------------------------------------------------------------------------
MINT = "M10 13Q10 6 17 6H49.5Q56.5 6 56.5 13V32H10Z"
CORAL = "M10 41.5Q10 34.5 17 34.5H49.5Q56.5 34.5 56.5 41.5V55.2C40 55.2 24 47.2 10 47.2Z"
CREAM = "M59.5 21Q59.5 14 66.5 14H83Q90 14 90 21V41.2C80 44.2 68 54.8 59.5 54.8Z"
GREEN = ("M10 50C24 50 40 58 56 58C70 58 80 47 90 44V69Q90 73.4 86.6 75.8L54.4 95.4Q50 98 45.6 95.4"
         "L13.4 75.8Q10 73.4 10 69Z")


def symbol(fills: dict[str, str]) -> str:
    return (f'<path fill="{fills["mint"]}" d="{MINT}"/><path fill="{fills["cream"]}" d="{CREAM}"/>'
            f'<path fill="{fills["coral"]}" d="{CORAL}"/><path fill="{fills["green"]}" d="{GREEN}"/>')


def mono(colour: str) -> dict[str, str]:
    return {k: colour for k in ("mint", "cream", "coral", "green")}


def wordmark(text: str = "saku") -> tuple[str, float, float, float, float]:
    """Return (path d, xmin, ymin, xmax, ymax) in font units, y flipped (SVG down)."""
    f = TTFont(FONT)
    gs, cmap, hmtx = f.getGlyphSet(), f.getBestCmap(), f["hmtx"]
    upm = f["head"].unitsPerEm
    tracking = -0.01 * upm
    x, parts = 0.0, []
    bp = BoundsPen(gs)
    for ch in text:
        g = cmap[ord(ch)]
        pen = SVGPathPen(gs)
        gs[g].draw(TransformPen(pen, (1, 0, 0, -1, x, 0)))
        gs[g].draw(TransformPen(bp, (1, 0, 0, -1, x, 0)))
        parts.append(pen.getCommands())
        x += hmtx[g][0] + tracking
    xmin, ymin, xmax, ymax = bp.bounds
    return " ".join(parts), xmin, ymin, xmax, ymax


def svg(w: float, h: float, body: str, title: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.2f} {h:.2f}" role="img" aria-label="{title}">'
            f"<title>{title}</title>{body}</svg>\n")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    full = C
    files = {
        "saku-mark.svg": svg(100, 100, symbol(full), "Saku"),
        "saku-mark-black.svg": svg(100, 100, symbol(mono("#000000")), "Saku"),
        "saku-mark-white.svg": svg(100, 100, symbol(mono("#FFFFFF")), "Saku"),
    }
    # app icon: symbol on a rounded square in brand green; the pocket turns a darker green to stay visible
    icon_fills = dict(full, green="#0A4F3A")
    files["saku-app-icon.svg"] = svg(100, 100, f'<rect width="100" height="100" rx="22" fill="{C["green"]}"/>'
                                     f'<g transform="translate(16 15) scale(.68)">{symbol(icon_fills)}</g>', "Saku")
    # favicon: same as app icon, simplified radius for 16-32 px
    files["favicon.svg"] = svg(32, 32, f'<rect width="32" height="32" rx="7" fill="{C["green"]}"/>'
                               f'<g transform="translate(2 1.4) scale(.28)">{symbol(icon_fills)}</g>', "Saku")

    d, xmin, ymin, xmax, ymax = wordmark()
    cap = ymax - ymin                         # height of the letters incl. k ascender
    scale = 74 / cap                          # wordmark height ~ 74% of the 100-unit symbol
    ww = (xmax - xmin) * scale
    gap = 16
    W = 100 + gap + ww
    word_y = 96 - ymax * scale                # baseline aligned to the pocket's bottom area
    word_y = 18 - ymin * scale + (78 - cap * scale) / 2 + 4
    tf = f"translate({100 + gap - xmin * scale:.2f} {word_y:.2f}) scale({scale:.5f})"
    for name, fills, word in (("saku-lockup.svg", full, C["ink"]),
                              ("saku-lockup-on-dark.svg", full, "#F7F4EC"),
                              ("saku-lockup-black.svg", mono("#000000"), "#000000"),
                              ("saku-lockup-white.svg", mono("#FFFFFF"), "#FFFFFF")):
        files[name] = svg(W, 100, symbol(fills) + f'<path fill="{word}" transform="{tf}" d="{d}"/>', "Saku")

    for name, content in files.items():
        (OUT / name).write_text(content)
        print(f"{name:<26} {len(content):>6} bytes")
    (OUT / "NOTICE.md").write_text(
        "# Saku brand files\n\nSymbol: Saku project, concept by Yona Surya (2026-10-18), redrawn as vector.\n"
        "Wordmark letters: Nunito Black, SIL Open Font License 1.1 (see ../fonts/Nunito-OFL.txt), converted to outlines.\n\n"
        f"Colours: green {C['green']}, mint {C['mint']}, coral {C['coral']}, cream {C['cream']}, ink {C['ink']}.\n")


if __name__ == "__main__":
    main()
