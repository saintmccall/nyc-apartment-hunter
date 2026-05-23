"""Tests for health check and weekly summary."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.monitor import check_health, send_weekly_summary
from src.dedup import deduplicate, update_score
from tests.conftest import make_listing


# ---------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------

def test_health_ok_when_recent_listing(db, sample_config):
    email_cfg = sample_config["notifications"]["email"]
    email_cfg["enabled"] = True
    listing = make_listing(url="https://streeteasy.com/rental/health1")
    deduplicate(db, [listing])

    with patch("src.monitor._send") as mock_send:
        healthy = check_health(db, email_cfg)
    assert healthy is True
    mock_send.assert_not_called()


def test_health_alert_when_no_recent_listings(db, sample_config):
    email_cfg = sample_config["notifications"]["email"]
    email_cfg["enabled"] = True

    listing = make_listing(url="https://streeteasy.com/rental/old_health")
    new = deduplicate(db, [listing])
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    db["listings"].update(new[0].id, {"first_seen": stale, "last_seen": stale})

    with patch("src.monitor._send") as mock_send:
        healthy = check_health(db, email_cfg)
    assert healthy is False
    mock_send.assert_called_once()
    subject = mock_send.call_args[1]["subject"] if mock_send.call_args[1] else mock_send.call_args[0][1]
    assert "24 hours" in subject


def test_health_skips_when_email_disabled(db, sample_config):
    sample_config["notifications"]["email"]["enabled"] = False
    with patch("src.monitor._send") as mock_send:
        healthy = check_health(db, sample_config["notifications"]["email"])
    assert healthy is True
    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# send_weekly_summary
# ---------------------------------------------------------------------------

def test_weekly_summary_sent(db, sample_config):
    email_cfg = sample_config["notifications"]["email"]
    email_cfg["enabled"] = True

    for i in range(5):
        l = make_listing(url=f"https://streeteasy.com/rental/w{i}")
        new = deduplicate(db, [l])
        update_score(db, new[0].id, float(i + 5))

    with patch("src.monitor._send") as mock_send:
        sent = send_weekly_summary(db, email_cfg)
    assert sent is True
    mock_send.assert_called_once()
    subject = mock_send.call_args[0][1]
    assert "weekly" in subject.lower()


def test_weekly_summary_skips_empty_db(db, sample_config):
    email_cfg = sample_config["notifications"]["email"]
    email_cfg["enabled"] = True
    with patch("src.monitor._send") as mock_send:
        sent = send_weekly_summary(db, email_cfg)
    assert sent is False
    mock_send.assert_not_called()


def test_weekly_summary_top3_only(db, sample_config):
    email_cfg = sample_config["notifications"]["email"]
    email_cfg["enabled"] = True

    for i in range(10):
        l = make_listing(url=f"https://streeteasy.com/rental/top{i}")
        new = deduplicate(db, [l])
        update_score(db, new[0].id, float(i + 1))

    html_parts = []
    with patch("src.monitor._send") as mock_send:
        send_weekly_summary(db, email_cfg)
        html_parts = mock_send.call_args[0][0]

    # Scores are stored as floats but rendered as int — top 3 are 10, 9, 8
    assert "10/10" in html_parts
    assert "9/10" in html_parts
    assert "8/10" in html_parts
