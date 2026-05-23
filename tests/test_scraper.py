"""Tests for scraper parsing logic — no live HTTP calls."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.scraper import (
    StreetEasyScraper,
    ZillowScraper,
    RentHopScraper,
    _parse_price,
    _detect_broker_fee,
    _extract_neighborhood_from_address,
    scrape_source,
)


# ---------------------------------------------------------------------------
# _parse_price
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("$3,500/mo", 3500),
    ("$4,200", 4200),
    ("3000", 3000),
    ("No price", 0),
    ("", 0),
])
def test_parse_price(text, expected):
    assert _parse_price(text) == expected


# ---------------------------------------------------------------------------
# _detect_broker_fee
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("no fee apartment", False),
    ("no broker fee", False),
    ("broker fee required", True),
    ("one month broker's fee", True),
    ("great light, high ceilings", None),
])
def test_detect_broker_fee(text, expected):
    assert _detect_broker_fee(text) == expected


# ---------------------------------------------------------------------------
# _extract_neighborhood_from_address
# ---------------------------------------------------------------------------

def test_extract_neighborhood_known():
    result = _extract_neighborhood_from_address("500 W 23rd St, Chelsea, NY")
    assert result == "Chelsea"


def test_extract_neighborhood_unknown():
    result = _extract_neighborhood_from_address("100 Some Unknown St, NY")
    assert result == ""


# ---------------------------------------------------------------------------
# StreetEasyScraper.parse — HTML fixture
# ---------------------------------------------------------------------------

_SE_HTML = """
<html><body>
  <article class="listing-item" data-gtm-listing-id="SE-001">
    <a class="listingCard-title" href="/rental/SE-001">Sunny 1BR East Village</a>
    <span class="price">$3,200/mo</span>
    <address class="listingCard-address">55 E 3rd St</address>
    <span class="listingCard-hood">East Village</span>
    <p class="listingCard-description">Pre-war charm, no broker fee.</p>
  </article>
  <article class="listing-item" data-gtm-listing-id="SE-002">
    <a class="listingCard-title" href="/rental/SE-002">Chelsea Studio</a>
    <span class="price">$4,100/mo</span>
    <address class="listingCard-address">200 W 22nd St</address>
    <span class="listingCard-hood">Chelsea</span>
    <p class="listingCard-description">Modern finishes, broker fee applies.</p>
  </article>
</body></html>
"""


def test_streeteasy_parse_returns_listings():
    scraper = StreetEasyScraper()
    listings = scraper.parse(_SE_HTML, "https://streeteasy.com/for-rent/manhattan/east-village")
    assert len(listings) == 2


def test_streeteasy_parse_first_listing():
    scraper = StreetEasyScraper()
    listings = scraper.parse(_SE_HTML, "https://streeteasy.com/for-rent/manhattan/east-village")
    l = listings[0]
    assert l.source == "streeteasy"
    assert l.price == 3200
    assert l.neighborhood == "East Village"
    assert l.has_broker_fee is False


def test_streeteasy_parse_broker_fee_detected():
    scraper = StreetEasyScraper()
    listings = scraper.parse(_SE_HTML, "https://streeteasy.com/for-rent/manhattan/chelsea")
    assert listings[1].has_broker_fee is True


def test_streeteasy_parse_empty_html():
    scraper = StreetEasyScraper()
    listings = scraper.parse("<html><body></body></html>", "https://streeteasy.com/")
    assert listings == []


# ---------------------------------------------------------------------------
# ZillowScraper.parse
# ---------------------------------------------------------------------------

_ZILLOW_HTML = """
<html><body>
  <article data-test="property-card" id="zpid_12345">
    <address data-test="property-card-addr">88 Greenwich St, New York, NY 10006</address>
    <span data-test="property-card-price">$3,800/mo</span>
    <a href="/homedetails/88-Greenwich-St/12345_zpid/">View</a>
  </article>
</body></html>
"""


def test_zillow_parse_returns_listing():
    scraper = ZillowScraper()
    listings = scraper.parse(_ZILLOW_HTML, "https://www.zillow.com/manhattan-new-york-ny/rentals/")
    assert len(listings) == 1
    assert listings[0].price == 3800
    assert listings[0].source == "zillow"


# ---------------------------------------------------------------------------
# RentHopScraper.parse
# ---------------------------------------------------------------------------

_RENTHOP_HTML = """
<html><body>
  <div class="search-result" id="listing_999">
    <a class="listing-title" href="/listings/999">Gramercy 1BR</a>
    <span class="price">$4,000/mo</span>
    <span class="neighborhood">Gramercy</span>
    <p class="description">Quiet block, no broker fee.</p>
  </div>
</body></html>
"""


def test_renthop_parse_returns_listing():
    scraper = RentHopScraper()
    listings = scraper.parse(_RENTHOP_HTML, "https://www.renthop.com/search")
    assert len(listings) == 1
    l = listings[0]
    assert l.price == 4000
    assert l.neighborhood == "Gramercy"
    assert l.has_broker_fee is False


# ---------------------------------------------------------------------------
# scrape_source — unknown source returns []
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrape_source_unknown():
    result = await scrape_source({"name": "unknown_site", "enabled": True}, {})
    assert result == []


# ---------------------------------------------------------------------------
# scrape_source — HTTP errors are swallowed, return []
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrape_source_http_error():
    import httpx
    with patch("src.scraper.StreetEasyScraper.search", new_callable=AsyncMock) as mock_search:
        mock_search.side_effect = httpx.ConnectError("refused")
        result = await scrape_source({"name": "streeteasy", "enabled": True}, {"neighborhoods": ["Chelsea"]})
        assert result == []
