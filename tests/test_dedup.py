"""Tests for deduplication and DB helpers."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from src.dedup import deduplicate, update_score, mark_notified, expire_old, get_stats, _listing_id
from tests.conftest import make_listing


def test_new_listing_inserted(db):
    listing = make_listing(url="https://streeteasy.com/rental/111")
    new = deduplicate(db, [listing])
    assert len(new) == 1
    assert db["listings"].count == 1


def test_duplicate_not_returned(db):
    listing = make_listing(url="https://streeteasy.com/rental/222")
    deduplicate(db, [listing])
    new = deduplicate(db, [listing])
    assert new == []
    assert db["listings"].count == 1


def test_duplicate_updates_last_seen(db):
    listing = make_listing(url="https://streeteasy.com/rental/333")
    deduplicate(db, [listing])
    first_seen = list(db["listings"].rows)[0]["last_seen"]

    import time; time.sleep(0.01)
    deduplicate(db, [listing])
    last_seen = list(db["listings"].rows)[0]["last_seen"]

    assert last_seen >= first_seen


def test_listing_id_strips_query_string():
    a = make_listing(url="https://streeteasy.com/rental/444?ref=foo")
    b = make_listing(url="https://streeteasy.com/rental/444?ref=bar")
    assert _listing_id(a) == _listing_id(b)


def test_listing_id_strips_trailing_slash():
    a = make_listing(url="https://streeteasy.com/rental/555/")
    b = make_listing(url="https://streeteasy.com/rental/555")
    assert _listing_id(a) == _listing_id(b)


def test_stable_id_written_back(db):
    listing = make_listing(url="https://streeteasy.com/rental/666", id="old-id")
    new = deduplicate(db, [listing])
    assert new[0].id != "old-id"
    assert len(new[0].id) == 24


def test_update_score(db):
    listing = make_listing(url="https://streeteasy.com/rental/777")
    new = deduplicate(db, [listing])
    update_score(db, new[0].id, 8.5)
    row = list(db["listings"].rows)[0]
    assert row["score"] == 8.5


def test_mark_notified(db):
    listing = make_listing(url="https://streeteasy.com/rental/888")
    new = deduplicate(db, [listing])
    mark_notified(db, new[0].id)
    row = list(db["listings"].rows)[0]
    assert row["notified"] == 1


def test_expire_old_removes_stale(db):
    listing = make_listing(url="https://streeteasy.com/rental/old")
    deduplicate(db, [listing])
    stale = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    db["listings"].update(list(db["listings"].rows)[0]["id"], {"last_seen": stale})

    deleted = expire_old(db, days=30)
    assert deleted == 1
    assert db["listings"].count == 0


def test_expire_old_keeps_recent(db):
    listing = make_listing(url="https://streeteasy.com/rental/recent")
    deduplicate(db, [listing])
    deleted = expire_old(db, days=30)
    assert deleted == 0
    assert db["listings"].count == 1


def test_get_stats(db):
    for i in range(3):
        listing = make_listing(url=f"https://streeteasy.com/rental/{i}")
        new = deduplicate(db, [listing])
        update_score(db, new[0].id, float(i + 5))

    stats = get_stats(db)
    assert stats["total_seen"] == 3
    assert stats["new_today"] == 3
    assert stats["avg_score"] == pytest.approx((5.0 + 6.0 + 7.0) / 3, abs=0.01)


def test_get_stats_ignores_zero_scores(db):
    listing = make_listing(url="https://streeteasy.com/rental/unscored")
    deduplicate(db, [listing])
    stats = get_stats(db)
    assert stats["avg_score"] == 0.0
