"""Quick scrape test — prints the first 5 listings found, no scoring or email."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.scraper import scrape_source


async def main() -> None:
    cfg = load_config()
    all_listings = []
    for source in cfg["sources"]:
        if source.get("enabled"):
            print(f"Scraping {source['name']}…")
            listings = await scrape_source(source, cfg["search"])
            all_listings.extend(listings)
            print(f"  {len(listings)} found")
    print(f"\nTotal: {len(all_listings)} listing(s)")
    for l in all_listings[:5]:
        print(f"  [{l.source}] {l.title or l.address} — ${l.price:,}/mo — {l.neighborhood}")


asyncio.run(main())
