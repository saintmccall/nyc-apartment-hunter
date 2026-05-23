"""Tests for notifier — mock SMTP, no live email."""
from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock

from src.notifier import notify, queue_for_digest, flush_digest, _render
from tests.conftest import make_listing


def _item(score: int = 7, **kwargs) -> dict:
    return {
        "listing": make_listing(**kwargs),
        "result": {
            "score": score,
            "summary": "Solid listing with good light.",
            "red_flags": ["flex room"] if score < 7 else [],
            "highlights": ["no fee", "pre-war"],
        },
    }


# ---------------------------------------------------------------------------
# _render
# ---------------------------------------------------------------------------

def test_render_contains_price():
    html = _render([_item(score=8, price=3600)])
    assert "3,600" in html


def test_render_sorted_by_score_descending():
    items = [_item(score=6), _item(score=9), _item(score=7)]
    html = _render(items)
    # Score badges render as "N&thinsp;/&thinsp;10" — find each in order
    pos_9 = html.index("9&thinsp;")
    pos_7 = html.index("7&thinsp;")
    pos_6 = html.index("6&thinsp;")
    assert pos_9 < pos_7 < pos_6


def test_render_red_flag_in_output():
    html = _render([_item(score=5)])
    assert "flex room" in html


def test_render_highlight_in_output():
    html = _render([_item(score=8)])
    assert "no fee" in html


# ---------------------------------------------------------------------------
# notify — below threshold sends nothing
# ---------------------------------------------------------------------------

def test_notify_below_threshold_returns_empty(db, sample_config):
    sample_config["notifications"]["email"]["enabled"] = True
    items = [_item(score=5)]
    result = notify(items, sample_config, db=db)
    assert result == []


# ---------------------------------------------------------------------------
# notify — immediate mode
# ---------------------------------------------------------------------------

@patch("src.notifier._send")
def test_notify_immediate_sends_email(mock_send, sample_config):
    sample_config["notifications"]["email"]["enabled"] = True
    sample_config["notifications"]["email"]["digest"] = False
    items = [_item(score=8)]
    result = notify(items, sample_config)
    mock_send.assert_called_once()
    assert len(result) == 1


@patch("src.notifier._send")
def test_notify_disabled_sends_nothing(mock_send, sample_config):
    sample_config["notifications"]["email"]["enabled"] = False
    items = [_item(score=9)]
    result = notify(items, sample_config)
    mock_send.assert_not_called()
    assert result == []


# ---------------------------------------------------------------------------
# notify — digest mode
# ---------------------------------------------------------------------------

def test_notify_digest_queues_item(db, sample_config):
    sample_config["notifications"]["email"]["enabled"] = True
    sample_config["notifications"]["email"]["digest"] = True
    items = [_item(score=8)]
    notify(items, sample_config, db=db)
    assert db["digest_queue"].count == 1


def test_notify_digest_queues_only_qualified(db, sample_config):
    sample_config["notifications"]["email"]["enabled"] = True
    sample_config["notifications"]["email"]["digest"] = True
    items = [_item(score=4), _item(score=8), _item(score=9)]
    notify(items, sample_config, db=db)
    assert db["digest_queue"].count == 2


@patch("src.notifier._send")
def test_flush_digest_sends_and_clears(mock_send, db, sample_config):
    sample_config["notifications"]["email"]["enabled"] = True
    sample_config["notifications"]["email"]["digest"] = True
    items = [_item(score=8), _item(score=9)]
    notify(items, sample_config, db=db)

    count = flush_digest(db, sample_config["notifications"]["email"])
    assert count == 2
    mock_send.assert_called_once()
    assert db["digest_queue"].count == 0


@patch("src.notifier._send")
def test_flush_digest_empty_queue(mock_send, db, sample_config):
    count = flush_digest(db, sample_config["notifications"]["email"])
    assert count == 0
    mock_send.assert_not_called()
