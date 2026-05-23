"""Tests for scorer — mock Anthropic API, no real calls."""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch

from src.scorer import (
    _extract_json,
    _safe_result,
    _listing_block,
    _single_prompt,
    _batch_prompt,
    score_listings,
)
from tests.conftest import make_listing


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------

def test_extract_json_plain():
    raw = '{"score": 7, "summary": "Good value.", "red_flags": [], "highlights": ["natural light"]}'
    result = _extract_json(raw)
    assert result["score"] == 7


def test_extract_json_fenced():
    raw = '```json\n{"score": 8, "summary": "Great.", "red_flags": [], "highlights": []}\n```'
    result = _extract_json(raw)
    assert result["score"] == 8


def test_extract_json_fenced_no_lang():
    raw = '```\n{"score": 5}\n```'
    result = _extract_json(raw)
    assert result["score"] == 5


def test_extract_json_invalid_raises():
    with pytest.raises(json.JSONDecodeError):
        _extract_json("not json at all")


# ---------------------------------------------------------------------------
# _safe_result
# ---------------------------------------------------------------------------

def test_safe_result_valid():
    raw = {"score": 9, "summary": "Excellent.", "red_flags": ["flex room"], "highlights": ["no fee"]}
    result = _safe_result(raw, "abc")
    assert result["score"] == 9
    assert result["red_flags"] == ["flex room"]


def test_safe_result_missing_fields():
    result = _safe_result({}, "abc")
    assert result["score"] == 0
    assert result["red_flags"] == []
    assert result["highlights"] == []


def test_safe_result_non_dict():
    result = _safe_result("oops", "abc")
    assert result["score"] == 0
    assert "parse error" in result["summary"].lower()


def test_safe_result_coerces_score_to_int():
    result = _safe_result({"score": "8"}, "abc")
    assert isinstance(result["score"], int)
    assert result["score"] == 8


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def test_listing_block_includes_price():
    l = make_listing(price=3750)
    block = _listing_block(l)
    assert "3750" in block


def test_listing_block_broker_fee_yes():
    l = make_listing(has_broker_fee=True)
    assert "Yes" in _listing_block(l)


def test_listing_block_broker_fee_no():
    l = make_listing(has_broker_fee=False)
    assert "No" in _listing_block(l)


def test_batch_prompt_contains_all_ids():
    listings = [make_listing(id=f"id{i}", url=f"https://streeteasy.com/{i}") for i in range(3)]
    prompt = _batch_prompt(listings)
    for i in range(3):
        assert f"id{i}" in prompt


# ---------------------------------------------------------------------------
# score_listings — mocked Anthropic client
# ---------------------------------------------------------------------------

def _mock_message(payload: dict | list) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload))]
    return msg


def _make_score_result(score: int = 7) -> dict:
    return {"score": score, "summary": "Good.", "red_flags": [], "highlights": []}


@patch("src.scorer.anthropic.Anthropic")
def test_score_single_listing(MockClient):
    MockClient.return_value.messages.create.return_value = _mock_message(_make_score_result(7))
    listing = make_listing()
    results = score_listings([listing], {"model": "claude-sonnet-4-20250514"})
    assert len(results) == 1
    assert results[0]["score"] == 7


@patch("src.scorer.anthropic.Anthropic")
def test_score_batch_mode(MockClient):
    batch_response = [_make_score_result(i + 5) for i in range(6)]
    MockClient.return_value.messages.create.return_value = _mock_message(batch_response)
    listings = [make_listing(id=f"id{i}", url=f"https://streeteasy.com/{i}") for i in range(6)]
    results = score_listings(listings, {"model": "claude-sonnet-4-20250514"})
    assert len(results) == 6
    assert results[0]["score"] == 5


@patch("src.scorer.anthropic.Anthropic")
def test_score_uses_cache(MockClient, db):
    from src.dedup import deduplicate, update_score
    listing = make_listing(url="https://streeteasy.com/rental/cached")
    new = deduplicate(db, [listing])
    update_score(db, new[0].id, 9.0)

    results = score_listings(new, {"model": "claude-sonnet-4-20250514"}, db=db)
    MockClient.return_value.messages.create.assert_not_called()
    assert results[0]["score"] == 9


@patch("src.scorer.anthropic.Anthropic")
def test_score_batch_fallback_on_count_mismatch(MockClient):
    # Batch returns wrong count → falls back to individual
    individual_response = _make_score_result(6)
    mock_client = MockClient.return_value
    # First call (batch) returns a 2-item array for 6 listings → mismatch
    # Subsequent individual calls return the correct response
    mock_client.messages.create.side_effect = [
        _mock_message([_make_score_result(6), _make_score_result(7)]),  # bad batch
    ] + [_mock_message(individual_response)] * 6

    listings = [make_listing(id=f"id{i}", url=f"https://streeteasy.com/{i}") for i in range(6)]
    results = score_listings(listings, {"model": "claude-sonnet-4-20250514"})
    assert len(results) == 6


@patch("src.scorer.anthropic.Anthropic")
def test_score_api_error_returns_zero(MockClient):
    MockClient.return_value.messages.create.side_effect = Exception("API down")
    listing = make_listing()
    results = score_listings([listing], {"model": "claude-sonnet-4-20250514"})
    assert results[0]["score"] == 0
