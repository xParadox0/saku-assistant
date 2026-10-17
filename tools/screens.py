#!/usr/bin/env python3
"""Screenshot the local demo dashboard pages (desktop + mobile) for visual QA. Read-only."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

EXE = "/home/yona/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell"
OUT = Path("/home/yona/projects/finance-assistant/docs/private/screens"); OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8090"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=EXE, args=["--no-sandbox"])
        for tag, vp in (("desktop", {"width": 1440, "height": 900}), ("mobile", {"width": 390, "height": 844})):
            ctx = await b.new_context(viewport=vp)
            page = await ctx.new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            for name in ("", "accounts", "imports", "admin"):
                r = await page.goto(f"{BASE}/{name}", wait_until="networkidle")
                f = OUT / f"{tag}-{name or 'overview'}.png"
                await page.screenshot(path=str(f), full_page=True)
                overflow = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
                print(tag, name or "overview", r.status, "overflow-x:", overflow, f)
            print(tag, "console errors:", errors)
            await ctx.close()
        await b.close()

asyncio.run(main())
