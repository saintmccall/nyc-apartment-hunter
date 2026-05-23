from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import urllib.robotparser
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Playwright availability check (optional dependency)
# ---------------------------------------------------------------------------

try:
    from playwright.async_api import async_playwright, Page
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    log.debug("playwright not installed — JS-rendered fallback disabled")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    id: str
    source: str
    url: str
    title: str
    price: int
    bedrooms: float
    neighborhood: str
    address: str
    description: str
    images: list[str] = field(default_factory=list)
    has_broker_fee: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# User-agent pool
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def _random_headers(referer: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


# ---------------------------------------------------------------------------
# Embedded JSON helpers
# ---------------------------------------------------------------------------

def _extract_next_data(html: str) -> dict | None:
    """Pull the __NEXT_DATA__ JSON blob that Next.js sites embed in every page."""
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError:
            pass
    return None


def _extract_initial_state(html: str) -> dict | None:
    """Extract window.__INITIAL_STATE__ or similar patterns from inline scripts."""
    patterns = [
        r'window\.__INITIAL_STATE__\s*=\s*({.+?});\s*</script>',
        r'window\.__STATE__\s*=\s*({.+?});\s*</script>',
        r'window\.__DATA__\s*=\s*({.+?});\s*</script>',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


# ---------------------------------------------------------------------------
# Playwright page fetcher
# ---------------------------------------------------------------------------

async def _playwright_fetch(url: str, wait_selector: str, timeout: int = 15000) -> str | None:
    """Render a page with a real browser and return the final HTML.

    Tries system Chrome first (already trusted by macOS Gatekeeper), then
    falls back to the downloaded Chromium headless shell.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        log.warning("Playwright not installed — run: .venv/bin/playwright install chromium")
        return None
    try:
        async with async_playwright() as pw:
            browser = await _launch_browser(pw)
            ctx = await browser.new_context(
                user_agent=random.choice(_USER_AGENTS),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = await ctx.new_page()
            await page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,avi}",
                lambda r: r.abort(),
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(wait_selector, timeout=timeout)
            except Exception:
                log.debug("Playwright: selector not found within %dms (%s)", timeout, wait_selector)
            html = await page.content()
            await browser.close()
            return html
    except Exception as exc:
        log.warning("Playwright fetch failed for %s: %s", url, exc)
        return None


async def _launch_browser(pw: Any) -> Any:
    """Try system Chrome first (macOS-trusted), fall back to downloaded Chromium."""
    # channel="chrome" uses the user's installed Google Chrome — always signed
    try:
        return await pw.chromium.launch(headless=True, channel="chrome")
    except Exception:
        pass
    # channel="chromium" uses Playwright's downloaded build
    try:
        return await pw.chromium.launch(headless=True, channel="chromium")
    except Exception:
        pass
    # Last resort: default launch (headless shell)
    return await pw.chromium.launch(headless=True)


# ---------------------------------------------------------------------------
# Base scraper
# ---------------------------------------------------------------------------

class Scraper(ABC):
    source_name: str
    base_url: str

    def __init__(self) -> None:
        self._robots: urllib.robotparser.RobotFileParser | None = None

    async def _load_robots(self, client: httpx.AsyncClient) -> None:
        robots_url = f"{self.base_url}/robots.txt"
        try:
            r = await client.get(robots_url, headers=_random_headers(), timeout=10)
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(r.text.splitlines())
            self._robots = rp
        except Exception as exc:
            log.debug("Could not fetch robots.txt for %s: %s", self.source_name, exc)

    def _allowed(self, url: str) -> bool:
        if self._robots is None:
            return True
        return self._robots.can_fetch("*", url)

    async def fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        referer: str = "",
        delay: float | None = None,
    ) -> str | None:
        if not self._allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        pause = delay if delay is not None else random.uniform(2.0, 3.0)
        await asyncio.sleep(pause)
        try:
            r = await client.get(url, headers=_random_headers(referer), timeout=20, follow_redirects=True)
            r.raise_for_status()
            return r.text
        except httpx.HTTPStatusError as exc:
            log.warning("%s HTTP %s for %s", self.source_name, exc.response.status_code, url)
        except Exception as exc:
            log.warning("%s fetch error for %s: %s", self.source_name, url, exc)
        return None

    @abstractmethod
    async def search(self, criteria: dict[str, Any]) -> list[Listing]:
        ...

    @abstractmethod
    def parse(self, html: str, page_url: str) -> list[Listing]:
        ...

    async def run(self, criteria: dict[str, Any]) -> list[Listing]:
        async with httpx.AsyncClient() as client:
            await self._load_robots(client)
            try:
                return await self.search(criteria)
            except Exception as exc:
                log.error("%s scraper failed: %s", self.source_name, exc, exc_info=True)
                return []


# ---------------------------------------------------------------------------
# StreetEasy
# ---------------------------------------------------------------------------

_SE_NEIGHBORHOOD_SLUGS: dict[str, str] = {
    "Lower East Side": "lower-east-side",
    "Greenwich Village": "greenwich-village",
    "NoLita": "nolita",
    "East Village": "east-village",
    "West Village": "west-village",
    "NoHo": "noho",
    "SoHo": "soho",
    "Chelsea": "chelsea",
    "Flatiron": "flatiron",
    "Murray Hill": "murray-hill",
    "Midtown East": "midtown-east",
    "Midtown West": "midtown-west",
    "Kips Bay": "kips-bay",
    "Gramercy": "gramercy",
    "Hell's Kitchen": "hells-kitchen",
    "Tudor City": "tudor-city",
    "Upper East Side": "upper-east-side",
}

# Selectors tried in order; StreetEasy's classes shift with deploys
_SE_CARD_SELECTORS = [
    "div[data-gtm-listing-id]",
    "article.re-SearchResults__listingCard",
    "li.re-SearchResults__listingItem",
    "article.listing-item",
]
_SE_WAIT_SELECTOR = "div[data-gtm-listing-id], article.re-SearchResults__listingCard"


class StreetEasyScraper(Scraper):
    source_name = "streeteasy"
    base_url = "https://streeteasy.com"

    async def search(self, criteria: dict[str, Any]) -> list[Listing]:
        neighborhoods = criteria.get("neighborhoods", list(_SE_NEIGHBORHOOD_SLUGS.keys()))
        price_min = criteria.get("price_min", 3000)
        price_max = criteria.get("price_max", 4500)

        # Build all URLs first
        hood_urls: list[tuple[str, str]] = []
        for hood in neighborhoods:
            slug = _SE_NEIGHBORHOOD_SLUGS.get(hood)
            if not slug:
                continue
            params = urlencode({"bedrooms": "1", "price": f"{price_min}-{price_max}", "sort_by": "listed_desc"})
            hood_urls.append((hood, f"{self.base_url}/for-rent/manhattan/{slug}?{params}"))

        # Layer 1: httpx for all neighborhoods
        needs_playwright: list[tuple[str, str]] = []
        listings: list[Listing] = []

        async with httpx.AsyncClient() as client:
            await self._load_robots(client)
            for hood, url in hood_urls:
                log.info("StreetEasy: fetching %s", url)
                html = await self.fetch(client, url, referer=self.base_url)
                found: list[Listing] = []
                if html:
                    found = self._parse_embedded_json(html, hood) or self.parse(html, url)
                if found:
                    for l in found:
                        l.neighborhood = l.neighborhood or hood
                    listings.extend(found)
                    log.info("StreetEasy %s: %d listing(s)", hood, len(found))
                else:
                    needs_playwright.append((hood, url))

        # Layer 2: one shared browser for all neighborhoods that need it
        if needs_playwright:
            log.info("StreetEasy: %d neighborhood(s) need Playwright", len(needs_playwright))
            pw_listings = await self._playwright_search(needs_playwright)
            listings.extend(pw_listings)

        return listings

    async def _playwright_search(self, hood_urls: list[tuple[str, str]]) -> list[Listing]:
        """Scrape multiple neighborhood pages with a single shared browser instance."""
        if not _PLAYWRIGHT_AVAILABLE:
            log.warning("Playwright not available — skipping JS fallback for %d neighborhoods", len(hood_urls))
            return []
        listings: list[Listing] = []
        try:
            async with async_playwright() as pw:
                browser = await _launch_browser(pw)
                ctx = await browser.new_context(
                    user_agent=random.choice(_USER_AGENTS),
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                page = await ctx.new_page()
                await page.route(
                    "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4}",
                    lambda r: r.abort(),
                )
                for hood, url in hood_urls:
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        try:
                            await page.wait_for_selector(_SE_WAIT_SELECTOR, timeout=12000)
                        except Exception:
                            log.debug("StreetEasy Playwright: selector not found for %s", hood)
                        html = await page.content()
                        found = self._parse_embedded_json(html, hood) or self.parse(html, url)
                        for l in found:
                            l.neighborhood = l.neighborhood or hood
                        listings.extend(found)
                        log.info("StreetEasy Playwright %s: %d listing(s)", hood, len(found))
                        await asyncio.sleep(random.uniform(2.0, 3.0))
                    except Exception as exc:
                        log.warning("StreetEasy Playwright failed for %s: %s", hood, exc)
                await browser.close()
        except Exception as exc:
            log.warning("StreetEasy Playwright browser launch failed: %s", exc)
        return listings

    def _parse_embedded_json(self, html: str, hood: str) -> list[Listing]:
        """Try to extract listings from StreetEasy's embedded page state."""
        data = _extract_initial_state(html) or _extract_next_data(html)
        if not data:
            return []
        # StreetEasy embeds listings under various keys depending on page version
        candidates = [
            _deep_get(data, "listings"),
            _deep_get(data, "props", "pageProps", "listings"),
            _deep_get(data, "searchResults", "listings"),
        ]
        raw_list = next((c for c in candidates if isinstance(c, list) and c), None)
        if not raw_list:
            return []
        listings = []
        for item in raw_list:
            try:
                listings.append(self._from_json(item, hood))
            except Exception as exc:
                log.debug("StreetEasy JSON listing parse error: %s", exc)
        log.info("StreetEasy %s: parsed %d listing(s) from embedded JSON", hood, len(listings))
        return listings

    def _from_json(self, item: dict, hood: str) -> Listing:
        lid = str(item.get("id") or item.get("listing_id") or "")
        url = item.get("url") or item.get("listing_url") or ""
        if not url.startswith("http"):
            url = f"{self.base_url}{url}"
        if not lid:
            lid = "se-" + hashlib.md5(url.encode()).hexdigest()[:12]
        price = _parse_price(str(item.get("price") or item.get("asking_price") or 0))
        text = json.dumps(item).lower()
        return Listing(
            id=lid,
            source="streeteasy",
            url=url,
            title=item.get("name") or item.get("address") or "",
            price=price,
            bedrooms=float(item.get("bedrooms") or 1),
            neighborhood=item.get("neighborhood") or hood,
            address=item.get("address") or "",
            description=item.get("description") or "",
            has_broker_fee=_detect_broker_fee(text),
        )

    def parse(self, html: str, page_url: str) -> list[Listing]:
        """DOM parser — used when embedded JSON is absent."""
        soup = BeautifulSoup(html, "lxml")
        cards: list[Tag] = []
        for sel in _SE_CARD_SELECTORS:
            cards = soup.select(sel)
            if cards:
                break
        listings = []
        for card in cards:
            try:
                listings.append(self._parse_card(card, page_url))
            except Exception as exc:
                log.debug("StreetEasy DOM card error: %s", exc)
        return listings

    def _parse_card(self, card: Tag, page_url: str) -> Listing:
        listing_id = card.get("data-gtm-listing-id") or card.get("data-id") or ""

        link_tag = (
            card.select_one("a.re-ListingCard__link")
            or card.select_one("a[href*='/rental/']")
            or card.select_one("a.listingCard-title")
        )
        href = link_tag["href"] if link_tag else ""
        url = href if href.startswith("http") else f"{self.base_url}{href}"
        if not listing_id:
            listing_id = "se-" + hashlib.md5(url.encode()).hexdigest()[:12]

        title_tag = (
            card.select_one("h3.re-ListingCard__title")
            or card.select_one("a.re-ListingCard__link")
            or card.select_one("a.listingCard-title")
        )
        title = title_tag.get_text(strip=True) if title_tag else ""

        price_tag = (
            card.select_one(".re-ListingCard__price")
            or card.select_one("[data-testid='listing-price']")
            or card.select_one(".price")
        )
        price = _parse_price(price_tag.get_text(strip=True)) if price_tag else 0

        addr_tag = card.select_one("address, .re-ListingCard__address, .listingCard-address")
        address = addr_tag.get_text(strip=True) if addr_tag else ""

        hood_tag = card.select_one(".re-ListingCard__hood, .listingCard-hood, .neighborhood")
        neighborhood = hood_tag.get_text(strip=True) if hood_tag else ""

        desc_tag = card.select_one(".re-ListingCard__details, .listingCard-description")
        description = desc_tag.get_text(strip=True) if desc_tag else ""

        images = [img["src"] for img in card.select("img[src]") if "placeholder" not in img.get("src", "")]

        return Listing(
            id=str(listing_id),
            source="streeteasy",
            url=url,
            title=title,
            price=price,
            bedrooms=1.0,
            neighborhood=neighborhood,
            address=address,
            description=description,
            images=images,
            has_broker_fee=_detect_broker_fee(card.get_text().lower()),
        )


# ---------------------------------------------------------------------------
# Zillow — primary: __NEXT_DATA__ JSON; fallback: DOM
# ---------------------------------------------------------------------------

class ZillowScraper(Scraper):
    source_name = "zillow"
    base_url = "https://www.zillow.com"

    # Zillow's __NEXT_DATA__ nests results under several possible paths
    _RESULT_PATHS = [
        ("props", "pageProps", "searchPageState", "cat1", "searchResults", "listResults"),
        ("props", "pageProps", "searchPageState", "cat1", "searchResults", "mapResults"),
        ("props", "pageProps", "initialReduxState", "listings", "listingsByQuery"),
    ]

    async def search(self, criteria: dict[str, Any]) -> list[Listing]:
        price_min = criteria.get("price_min", 3000)
        price_max = criteria.get("price_max", 4500)

        import json as _json
        search_state = _json.dumps({
            "pagination": {},
            "isMapVisible": False,
            "filterState": {
                "beds": {"min": 1, "max": 1},
                "price": {"min": price_min, "max": price_max},
                "fr": {"value": True},
                "fsba": {"value": False},
                "fsbo": {"value": False},
                "nc": {"value": False},
                "cmsn": {"value": False},
                "auc": {"value": False},
                "fore": {"value": False},
            },
            "isListVisible": True,
        })
        from urllib.parse import quote
        url = f"{self.base_url}/manhattan-new-york-ny/rentals/?searchQueryState={quote(search_state)}"
        log.info("Zillow: fetching %s", url[:120] + "…")

        async with httpx.AsyncClient() as client:
            await self._load_robots(client)
            html = await self.fetch(client, url, referer=self.base_url)

        if not html:
            return []

        listings = self.parse(html, url)
        if not listings:
            log.info("Zillow: no listings from __NEXT_DATA__ — trying Playwright")
            html = await _playwright_fetch(url, "article[data-test='property-card']")
            if html:
                listings = self.parse(html, url)
        return listings

    def parse(self, html: str, page_url: str) -> list[Listing]:
        # Primary: __NEXT_DATA__ JSON
        data = _extract_next_data(html)
        if data:
            for path in self._RESULT_PATHS:
                raw_list = _deep_get(data, *path)
                if isinstance(raw_list, list) and raw_list:
                    log.info("Zillow: found %d listing(s) in __NEXT_DATA__", len(raw_list))
                    return [r for r in (_zillow_from_json(item, self.base_url) for item in raw_list) if r]

        # Fallback: DOM parsing
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("article[data-test='property-card'], li[id^='zpid']")
        listings = []
        for card in cards:
            try:
                listings.append(self._parse_card(card, page_url))
            except Exception as exc:
                log.debug("Zillow DOM card error: %s", exc)
        return listings

    def _parse_card(self, card: Tag, page_url: str) -> Listing:
        zpid = card.get("id", "") or card.get("data-zpid", "")
        listing_id = f"zl-{zpid}" if zpid else "zl-" + hashlib.md5(page_url.encode()).hexdigest()[:12]

        address_tag = card.select_one("address, [data-test='property-card-addr']")
        address = address_tag.get_text(strip=True) if address_tag else ""

        price_tag = card.select_one("[data-test='property-card-price'], .list-card-price")
        price = _parse_price(price_tag.get_text(strip=True)) if price_tag else 0

        link_tag = card.select_one("a[href*='/homedetails/'], a[href*='/b/']")
        href = link_tag["href"] if link_tag else ""
        url = href if href.startswith("http") else f"{self.base_url}{href}"

        detail_tag = card.select_one("[data-test='property-card-details'], .list-card-details")
        description = detail_tag.get_text(" ", strip=True) if detail_tag else ""
        images = [img["src"] for img in card.select("img[src]") if "placeholder" not in img.get("src", "")]

        return Listing(
            id=listing_id,
            source="zillow",
            url=url,
            title=address,
            price=price,
            bedrooms=1.0,
            neighborhood=_extract_neighborhood_from_address(address),
            address=address,
            description=description,
            images=images,
            has_broker_fee=_detect_broker_fee(card.get_text().lower()),
        )


def _zillow_from_json(item: dict, base_url: str) -> Listing | None:
    """Parse a single entry from Zillow's listResults array."""
    try:
        zpid = str(item.get("zpid") or "")
        detail_url = item.get("detailUrl") or item.get("url") or ""
        url = detail_url if detail_url.startswith("http") else f"{base_url}{detail_url}"
        listing_id = f"zl-{zpid}" if zpid else "zl-" + hashlib.md5(url.encode()).hexdigest()[:12]

        # Price can live in several fields
        price_raw = (
            item.get("unformattedPrice")
            or item.get("price")
            or _deep_get(item, "hdpData", "homeInfo", "price")
            or 0
        )
        price = _parse_price(str(price_raw))

        address = item.get("address") or item.get("streetAddress") or ""
        beds = float(item.get("beds") or _deep_get(item, "hdpData", "homeInfo", "bedrooms") or 1)
        description = item.get("statusText") or ""

        text = json.dumps(item).lower()
        return Listing(
            id=listing_id,
            source="zillow",
            url=url,
            title=address,
            price=price,
            bedrooms=beds,
            neighborhood=_extract_neighborhood_from_address(address),
            address=address,
            description=description,
            images=[item.get("imgSrc")] if item.get("imgSrc") else [],
            has_broker_fee=_detect_broker_fee(text),
        )
    except Exception as exc:
        log.debug("Zillow JSON item parse error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# RentHop — improved selectors + Playwright fallback
# ---------------------------------------------------------------------------

_RH_CARD_SELECTORS = [
    "div.search-result-list-item",
    "div.listing-short",
    "div[id^='listing_']",
    "div.search-result",
]
_RH_WAIT_SELECTOR = "div.search-result-list-item, div.listing-short, div[id^='listing_']"


class RentHopScraper(Scraper):
    source_name = "renthop"
    base_url = "https://www.renthop.com"

    async def search(self, criteria: dict[str, Any]) -> list[Listing]:
        price_min = criteria.get("price_min", 3000)
        price_max = criteria.get("price_max", 4500)

        params = urlencode({
            "min_price": price_min,
            "max_price": price_max,
            "min_bedrooms": 1,
            "max_bedrooms": 1,
            "sort": "hoppiness",
            "borough[]": "manhattan",
        })
        url = f"{self.base_url}/search?{params}"
        log.info("RentHop: fetching %s", url)

        async with httpx.AsyncClient() as client:
            await self._load_robots(client)
            html = await self.fetch(client, url, referer=self.base_url)

        listings: list[Listing] = []
        if html:
            listings = self.parse(html, url)

        if not listings:
            log.info("RentHop: 0 results via httpx — trying Playwright")
            html = await _playwright_fetch(url, _RH_WAIT_SELECTOR)
            if html:
                listings = self.parse(html, url)

        return listings

    def parse(self, html: str, page_url: str) -> list[Listing]:
        soup = BeautifulSoup(html, "lxml")
        cards: list[Tag] = []
        for sel in _RH_CARD_SELECTORS:
            cards = soup.select(sel)
            if cards:
                break
        listings = []
        for card in cards:
            try:
                listings.append(self._parse_card(card, page_url))
            except Exception as exc:
                log.debug("RentHop card parse error: %s", exc)
        return listings

    def _parse_card(self, card: Tag, page_url: str) -> Listing:
        card_id = card.get("id", "")
        listing_id = f"rh-{card_id}" if card_id else "rh-" + hashlib.md5(page_url.encode()).hexdigest()[:12]

        link_tag = (
            card.select_one("a.listing-title")
            or card.select_one("h2 a")
            or card.select_one("h3 a")
            or card.select_one(".address a")
        )
        title = link_tag.get_text(strip=True) if link_tag else ""
        href = link_tag["href"] if link_tag and link_tag.has_attr("href") else ""
        url = href if href.startswith("http") else f"{self.base_url}{href}"

        price_tag = (
            card.select_one("div.price-info")
            or card.select_one("span.listing-price-amount")
            or card.select_one(".price")
        )
        price = _parse_price(price_tag.get_text(strip=True)) if price_tag else 0

        addr_tag = card.select_one("div.address, .listing-address, h2")
        address = addr_tag.get_text(strip=True) if addr_tag else title

        hood_tag = card.select_one(".neighborhood, .listing-neighborhood, .hood")
        neighborhood = (
            hood_tag.get_text(strip=True) if hood_tag
            else _extract_neighborhood_from_address(address)
        )

        desc_tag = card.select_one(".description, .listing-description, .details")
        description = desc_tag.get_text(strip=True) if desc_tag else ""
        images = [img["src"] for img in card.select("img[src]") if "placeholder" not in img.get("src", "")]

        return Listing(
            id=listing_id,
            source="renthop",
            url=url,
            title=title,
            price=price,
            bedrooms=1.0,
            neighborhood=neighborhood,
            address=address,
            description=description,
            images=images,
            has_broker_fee=_detect_broker_fee(card.get_text().lower()),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_price(text: str) -> int:
    digits = "".join(c for c in text if c.isdigit())
    return int(digits) if digits else 0


def _detect_broker_fee(text: str) -> bool | None:
    if "no fee" in text or "no broker" in text:
        return False
    if "broker fee" in text or "broker's fee" in text or "one month fee" in text:
        return True
    return None


def _extract_neighborhood_from_address(address: str) -> str:
    for hood in _SE_NEIGHBORHOOD_SLUGS:
        if hood.lower() in address.lower():
            return hood
    return ""


def _deep_get(obj: Any, *keys: str) -> Any:
    """Safe nested dict access: _deep_get(d, 'a', 'b', 'c') == d['a']['b']['c']."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


# ---------------------------------------------------------------------------
# Craigslist — public RSS feed, no browser needed
# ---------------------------------------------------------------------------

# Manhattan Craigslist neighborhoods that map to our target areas
_CL_AREAS = [
    "mnh",   # Manhattan (general)
]

_CL_HOODS = [
    "lower east side", "greenwich village", "nolita", "east village",
    "west village", "noho", "soho", "chelsea", "flatiron", "murray hill",
    "midtown east", "midtown west", "kips bay", "gramercy", "hell's kitchen",
    "tudor city", "upper east side",
]


class CraigslistScraper(Scraper):
    source_name = "craigslist"
    base_url = "https://newyork.craigslist.org"

    async def search(self, criteria: dict[str, Any]) -> list[Listing]:
        price_min = criteria.get("price_min", 3000)
        price_max = criteria.get("price_max", 4500)
        listings: list[Listing] = []

        async with httpx.AsyncClient() as client:
            await self._load_robots(client)
            for area in _CL_AREAS:
                params = urlencode({
                    "format": "rss",
                    "bedrooms": 1,
                    "min_price": price_min,
                    "max_price": price_max,
                    "availabilityMode": 0,
                    "sale_date": "all+dates",
                })
                url = f"{self.base_url}/search/{area}/apa?{params}"
                log.info("Craigslist: fetching %s", url)
                html = await self.fetch(client, url, referer=self.base_url, delay=1.5)
                if html:
                    found = self.parse(html, url)
                    # Filter to target neighborhoods
                    found = [l for l in found if self._in_target_hood(l)]
                    listings.extend(found)
                    log.info("Craigslist %s: %d listing(s) in target neighborhoods", area, len(found))
        return listings

    def parse(self, html: str, page_url: str) -> list[Listing]:
        """Parse Craigslist RSS feed (XML)."""
        listings = []
        try:
            root = ET.fromstring(html)
            ns = {"rss": "http://purl.org/rss/1.0/", "cl": "http://www.craigslist.org/about/cl-rss"}
            channel = root.find("channel") or root.find("rss:channel", ns)
            items = root.findall(".//item") or root.findall(".//rss:item", ns)
            for item in items:
                try:
                    listings.append(self._parse_item(item, ns))
                except Exception as exc:
                    log.debug("Craigslist item parse error: %s", exc)
        except ET.ParseError as exc:
            log.warning("Craigslist XML parse error: %s", exc)
        return listings

    def _parse_item(self, item: ET.Element, ns: dict) -> Listing:
        def text(tag: str) -> str:
            el = item.find(tag) or item.find(f"rss:{tag}", ns)
            return (el.text or "").strip() if el is not None else ""

        title = text("title")
        url = text("link")
        description = BeautifulSoup(text("description"), "lxml").get_text(" ", strip=True)
        pub_date = text("pubDate")

        # Price from title e.g. "$3,200 / 1br"
        price = _parse_price(title)

        # Craigslist embeds geo in the description or title
        address = ""
        geo = item.find("{http://www.w3.org/2003/01/geo/wgs84_pos#}Point")
        if geo is None:
            # Try pulling address from description text
            addr_match = re.search(r"\(([^)]{5,60})\)", description)
            address = addr_match.group(1) if addr_match else ""

        # Derive listing ID from URL
        listing_id = "cl-" + hashlib.sha256(url.split("?")[0].encode()).hexdigest()[:16]

        neighborhood = _extract_neighborhood_from_address(title + " " + description)

        text_lower = (title + " " + description).lower()
        return Listing(
            id=listing_id,
            source="craigslist",
            url=url,
            title=title,
            price=price,
            bedrooms=1.0,
            neighborhood=neighborhood,
            address=address,
            description=description,
            has_broker_fee=_detect_broker_fee(text_lower),
        )

    def _in_target_hood(self, listing: Listing) -> bool:
        if listing.neighborhood:
            return True
        # Accept listings where neighborhood appears in title or description
        combined = (listing.title + " " + listing.description).lower()
        return any(h in combined for h in _CL_HOODS)


# ---------------------------------------------------------------------------
# Debug helper — inspect raw HTML from any source
# ---------------------------------------------------------------------------

async def debug_fetch(source_name: str, criteria: dict[str, Any]) -> None:
    """Fetch one page and dump what we got — useful for selector debugging."""
    import tempfile, os
    scrapers: dict[str, Scraper] = {
        "streeteasy": StreetEasyScraper(),
        "zillow": ZillowScraper(),
        "renthop": RentHopScraper(),
        "craigslist": CraigslistScraper(),
    }
    scraper = scrapers.get(source_name)
    if not scraper:
        print(f"Unknown source: {source_name}")
        return

    price_min = criteria.get("price_min", 3000)
    price_max = criteria.get("price_max", 4500)

    urls: dict[str, str] = {
        "streeteasy": f"https://streeteasy.com/for-rent/manhattan/east-village?bedrooms=1&price={price_min}-{price_max}",
        "zillow": f"https://www.zillow.com/manhattan-new-york-ny/rentals/",
        "renthop": f"https://www.renthop.com/search?min_price={price_min}&max_price={price_max}&min_bedrooms=1&max_bedrooms=1&borough[]=manhattan",
        "craigslist": f"https://newyork.craigslist.org/search/mnh/apa?format=rss&bedrooms=1&min_price={price_min}&max_price={price_max}",
    }
    url = urls.get(source_name, "")
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=_random_headers(), timeout=20, follow_redirects=True)
    path = os.path.join(tempfile.gettempdir(), f"debug_{source_name}.html")
    with open(path, "w") as f:
        f.write(r.text)
    print(f"HTTP {r.status_code} — {len(r.text):,} bytes")
    print(f"Saved to: {path}")
    # Quick content sniff
    has_next = "__NEXT_DATA__" in r.text
    has_init = "__INITIAL_STATE__" in r.text
    print(f"__NEXT_DATA__: {has_next}  |  __INITIAL_STATE__: {has_init}")
    soup = BeautifulSoup(r.text, "lxml")
    scripts = soup.find_all("script")
    print(f"Script tags: {len(scripts)}  |  Total HTML size: {len(r.text):,} chars")


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

_SCRAPERS: dict[str, type[Scraper]] = {
    "streeteasy": StreetEasyScraper,
    "zillow": ZillowScraper,
    "renthop": RentHopScraper,
    "craigslist": CraigslistScraper,
}


async def scrape_source(source: dict[str, Any], criteria: dict[str, Any]) -> list[Listing]:
    name = source["name"]
    scraper_cls = _SCRAPERS.get(name)
    if scraper_cls is None:
        log.warning("No scraper implemented for source: %s", name)
        return []
    return await scraper_cls().run(criteria)
