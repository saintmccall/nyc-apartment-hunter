from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

import sqlite_utils

from .scraper import Listing

log = logging.getLogger(__name__)

DB_PATH = Path("data/listings.db")

_SCHEMA = {
    "id": str,          # SHA-256 of the canonical URL
    "url": str,
    "source": str,
    "first_seen": str,  # ISO-8601 UTC
    "last_seen": str,   # ISO-8601 UTC
    "title": str,
    "price": int,
    "bedrooms": float,
    "neighborhood": str,
    "address": str,
    "description": str,
    "score": float,
    "notified": int,    # 0 / 1 — sqlite has no bool type
}


class Stats(TypedDict):
    total_seen: int
    new_today: int
    avg_score: float


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

def get_db(path: Path = DB_PATH) -> sqlite_utils.Database:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite_utils.Database(path)
    table = db["listings"]
    if "listings" not in db.table_names():
        table.create(_SCHEMA, pk="id")
        table.create_index(["source"])
        table.create_index(["first_seen"])
        table.create_index(["score"])
    return db


# ---------------------------------------------------------------------------
# ID derivation
# ---------------------------------------------------------------------------

def _listing_id(listing: Listing) -> str:
    # Normalise the URL so minor query-string differences don't create dupes
    canonical = listing.url.split("?")[0].rstrip("/").lower()
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Core dedup
# ---------------------------------------------------------------------------

def deduplicate(
    db: sqlite_utils.Database,
    listings: list[Listing],
) -> list[Listing]:
    """Return only the listings not already in the DB; update last_seen on the rest."""
    now = datetime.now(timezone.utc).isoformat()
    new: list[Listing] = []

    for listing in listings:
        lid = _listing_id(listing)
        existing = list(db["listings"].rows_where("id = ?", [lid]))

        if existing:
            db["listings"].update(lid, {"last_seen": now})
            log.debug("Already seen %s (%s)", lid, listing.url)
        else:
            db["listings"].insert({
                "id": lid,
                "url": listing.url,
                "source": listing.source,
                "first_seen": now,
                "last_seen": now,
                "title": listing.title,
                "price": listing.price,
                "bedrooms": listing.bedrooms,
                "neighborhood": listing.neighborhood,
                "address": listing.address,
                "description": listing.description,
                "score": 0.0,
                "notified": 0,
            })
            # Propagate the stable DB id back onto the listing object
            listing.id = lid
            new.append(listing)
            log.debug("New listing %s from %s", lid, listing.source)

    log.info("Dedup: %d new / %d already seen", len(new), len(listings) - len(new))
    return new


# ---------------------------------------------------------------------------
# Score write-back
# ---------------------------------------------------------------------------

def update_score(db: sqlite_utils.Database, listing_id: str, score: float) -> None:
    db["listings"].update(listing_id, {"score": score})


def mark_notified(db: sqlite_utils.Database, listing_id: str) -> None:
    db["listings"].update(listing_id, {"notified": 1})


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def expire_old(db: sqlite_utils.Database, days: int = 30) -> int:
    """Delete listings whose last_seen is older than `days`. Returns count deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    before = db["listings"].count
    db["listings"].delete_where("last_seen < ?", [cutoff])
    deleted = before - db["listings"].count
    if deleted:
        log.info("Expired %d listing(s) older than %d days", deleted, days)
    return deleted


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_stats(db: sqlite_utils.Database) -> Stats:
    today = datetime.now(timezone.utc).date().isoformat()

    total = db["listings"].count

    new_today = db["listings"].count_where("first_seen >= ?", [today])

    row = next(db.execute("SELECT AVG(score) FROM listings WHERE score > 0"), (None,))
    avg_score = round(float(row[0]), 2) if row[0] is not None else 0.0

    return Stats(total_seen=total, new_today=new_today, avg_score=avg_score)
