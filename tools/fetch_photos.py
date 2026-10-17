#!/usr/bin/env python3
"""Download the chosen dashboard photos from their Pinterest pins (originals size) for LOCAL preview.

The files land in saku/web/static/img/ which is gitignored: the source Unsplash pages could not be opened
by automation (401), so the licence (Unsplash License vs paid Unsplash+) is not verified yet. Commit them
only after a human opens each page in PROVENANCE and confirms "Free to use under the Unsplash License".

Run from the repo root: uv run --no-project --with pillow python tools/fetch_photos.py
"""
import io
import json
import re
import urllib.request
from pathlib import Path

from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "docs/private/pinterest/photos/free-source-pins.json"
OUT = ROOT / "saku/web/static/img"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
CHOSEN = {  # file stem -> (unsplash photo id as linked from the pin, where it is used)
    "hero-city": ("qegMLAiTBA4", "overview header band"),
    "welcome-lines": ("r4HT6aurLQw", "first-run welcome card and sidebar promise"),
}


def main() -> None:
    pins = json.load(open(PINS))
    OUT.mkdir(parents=True, exist_ok=True)
    prov = []
    for stem, (pid, use) in CHOSEN.items():
        pin = next(p for p in pins if pid in (p.get("link") or ""))
        url = re.sub(r"/\d+x/", "/originals/", pin["image"])
        try:
            data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40).read()
        except Exception:  # noqa: BLE001  originals not always present; fall back to the pin size
            data = urllib.request.urlopen(urllib.request.Request(pin["image"], headers=UA), timeout=40).read()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((1800, 1800))
        img = ImageEnhance.Color(img).enhance(0.7)  # colder, nearer the brand's near-monochrome photos
        img.save(OUT / f"{stem}.jpg", "JPEG", quality=78, optimize=True, progressive=True)
        title = pin.get("title") or ""
        author = (re.search(r"Photo by (.+?) on Unsplash", title) or [None, "unknown (not in pin metadata)"])[1]
        prov.append({"file": f"{stem}.jpg", "use": use, "size": f"{img.width}x{img.height}",
                     "unsplash_page": f"https://unsplash.com/photos/{pid}", "author_per_pin": author,
                     "pinterest_pin": pin.get("url"), "licence_status": "UNVERIFIED"})
        print(stem, img.size, (OUT / f"{stem}.jpg").stat().st_size, "bytes |", author)
    (OUT / "PROVENANCE.json").write_text(json.dumps(prov, indent=1) + "\n")


if __name__ == "__main__":
    main()
