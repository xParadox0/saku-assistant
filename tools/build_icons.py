#!/usr/bin/env python3
"""Build saku/web/templates/_icons.html from Phosphor Icons (MIT, github.com/phosphor-icons/core), duotone weight.

Run from the repo root: python3 tools/build_icons.py
Downloads only the icons listed in MAP, rewrites each <svg> into a <symbol>, and copies the MIT licence.
"""
import json
import os
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "phosphor-icons/core"
WEIGHT = "duotone"
MAP = {  # sprite id -> phosphor name
    "i-home": "house", "i-wallet": "wallet", "i-upload": "upload-simple", "i-pulse": "pulse",
    "i-food": "fork-knife", "i-basket": "basket", "i-ride": "motorcycle", "i-bolt": "lightning",
    "i-repeat": "arrows-clockwise", "i-bag": "shopping-bag", "i-heart": "heartbeat", "i-receipt": "receipt",
    "i-dots": "dots-three-circle", "i-arrow-in": "arrow-down-left", "i-arrow-out": "arrow-up-right",
    "i-swap": "arrows-left-right", "i-undo": "arrow-u-up-left", "i-spark": "sparkle", "i-alert": "warning",
    "i-calendar": "calendar-dots", "i-plus": "plus", "i-file": "file-text", "i-check": "check-circle",
    "i-x": "x-circle", "i-bank": "bank", "i-phone": "device-mobile", "i-cash": "money", "i-shield": "shield-check",
    "i-chevron": "caret-right", "i-coffee": "coffee", "i-chart": "chart-donut", "i-trend": "trend-up",
}


def get(url: str) -> bytes:
    headers = {"User-Agent": "saku-build-icons"}
    tok = Path(os.path.expanduser("~/.config/gh_token"))
    if "api.github.com" in url and tok.exists():
        headers["Authorization"] = "Bearer " + tok.read_text().strip()
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30).read()


def main() -> None:
    sha = json.loads(get(f"https://api.github.com/repos/{REPO}/commits/main"))["sha"]
    raw = f"https://raw.githubusercontent.com/{REPO}/{sha}"
    symbols, missing = [], []
    for sid, name in MAP.items():
        try:
            svg = get(f"{raw}/assets/{WEIGHT}/{name}-{WEIGHT}.svg").decode()
        except Exception as e:  # noqa: BLE001
            missing.append(f"{name}: {e}")
            continue
        body = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1).strip()
        body = re.sub(r'\s(fill|stroke)="#?[0-9A-Fa-f]{3,6}"', "", body)  # inherit currentColor
        symbols.append(f'  <symbol id="{sid}" viewBox="0 0 256 256">{body}</symbol>')
    if missing:
        raise SystemExit("missing icons:\n" + "\n".join(missing))
    out = ROOT / "saku/web/templates/_icons.html"
    out.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" class="sprite" aria-hidden="true" focusable="false">\n'
        f"  <!-- Phosphor Icons, {WEIGHT} weight. MIT licence, see saku/web/static/icons/LICENSE-phosphor.txt. "
        f"Source: github.com/{REPO} @ {sha[:7]} -->\n" + "\n".join(symbols) + "\n</svg>\n")
    lic = ROOT / "saku/web/static/icons"
    lic.mkdir(parents=True, exist_ok=True)
    (lic / "LICENSE-phosphor.txt").write_bytes(get(f"{raw}/LICENSE"))
    print(f"{len(symbols)} icons from {REPO}@{sha[:7]} -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
