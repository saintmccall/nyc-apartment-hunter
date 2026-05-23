"""Shared fixtures for all test modules."""
from __future__ import annotations

import pytest
import sqlite_utils

from src.scraper import Listing
from src.dedup import get_db


# ---------------------------------------------------------------------------
# Listing factory
# ---------------------------------------------------------------------------

def make_listing(
    *,
    id: str = "abc123",
    source: str = "streeteasy",
    url: str = "https://streeteasy.com/rental/123",
    title: str = "Sunny 1BR in East Village",
    price: int = 3500,
    bedrooms: float = 1.0,
    neighborhood: str = "East Village",
    address: str = "123 E 7th St, New York, NY 10009",
    description: str = "Beautiful pre-war 1BR with exposed brick.",
    has_broker_fee: bool | None = False,
) -> Listing:
    return Listing(
        id=id,
        source=source,
        url=url,
        title=title,
        price=price,
        bedrooms=bedrooms,
        neighborhood=neighborhood,
        address=address,
        description=description,
        has_broker_fee=has_broker_fee,
    )


@pytest.fixture
def listing() -> Listing:
    return make_listing()


@pytest.fixture
def listing_factory():
    return make_listing


@pytest.fixture
def db(tmp_path):
    """In-memory SQLite DB for each test."""
    return get_db(tmp_path / "test.db")


@pytest.fixture
def sample_config() -> dict:
    return {
        "search": {
            "borough": "Manhattan",
            "bedrooms": 1,
            "price_min": 3000,
            "price_max": 4500,
            "neighborhoods": ["East Village", "Chelsea"],
        },
        "sources": [
            {"name": "streeteasy", "enabled": True, "priority": 1},
        ],
        "schedule": {"interval_minutes": 10, "quiet_hours": "23:00-07:00"},
        "notifications": {
            "email": {
                "enabled": False,
                "address": "test@example.com",
                "app_password": "fake",
                "smtp_host": "localhost",
                "smtp_port": 1025,
                "min_score": 6,
                "digest": False,
            }
        },
        "scoring": {"model": "claude-sonnet-4-20250514", "evaluate": []},
    }
