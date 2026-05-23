"""Dump raw StreetEasy HTML from Playwright to /tmp/se_debug.html for inspection."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scraper import _launch_browser, _random_headers
from playwright.async_api import async_playwright


async def main() -> None:
    url = "https://streeteasy.com/for-rent/manhattan/east-village?bedrooms=1&price=3000-4500"
    print(f"Fetching {url} with Playwright...")
    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)  # let JS run
        html = await page.content()
        await browser.close()

    out = Path("/tmp/se_debug.html")
    out.write_text(html)
    print(f"Wrote {len(html):,} bytes to {out}")

    # Quick summary
    if "challenge" in html.lower() or "captcha" in html.lower():
        print(">>> CLOUDFLARE CHALLENGE detected")
    elif "data-gtm-listing-id" in html:
        print(">>> LISTINGS found in HTML (selector issue)")
    elif "__NEXT_DATA__" in html or "__INITIAL_STATE__" in html:
        print(">>> EMBEDDED JSON found (JSON parse issue)")
    else:
        print(">>> No listings, no challenge — page loaded but empty or different structure")
        print("First 500 chars of <body>:")
        import re
        body = re.search(r'<body[^>]*>(.*)', html, re.DOTALL)
        if body:
            print(body.group(1)[:500])


asyncio.run(main())
