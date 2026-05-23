from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import anthropic
import sqlite_utils

from .scraper import Listing

log = logging.getLogger(__name__)

_SYSTEM = """\
You are an expert NYC apartment analyst. Score this listing 1-10 based on:
- Value for price (compare to typical Manhattan 1BR at $3000-$4500)
- Listing quality and transparency (are they hiding anything?)
- Must-haves: No basement units
- Any standout features

Respond in JSON: { "score": int, "summary": string (2-3 sentences), "red_flags": string[], "highlights": string[] }"""

# Batch threshold: use multi-listing prompt when count exceeds this
_BATCH_THRESHOLD = 5

# How long to wait between individual API calls (seconds)
_RATE_DELAY = 0.3


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _listing_block(listing: Listing) -> str:
    broker = (
        "Yes" if listing.has_broker_fee is True
        else "No" if listing.has_broker_fee is False
        else "Unknown"
    )
    return (
        f"Source: {listing.source}\n"
        f"Title: {listing.title}\n"
        f"Price: ${listing.price}/mo\n"
        f"Bedrooms: {listing.bedrooms}\n"
        f"Neighborhood: {listing.neighborhood}\n"
        f"Address: {listing.address}\n"
        f"Broker fee: {broker}\n"
        f"Description: {listing.description or '(none provided)'}\n"
        f"URL: {listing.url}"
    )


def _single_prompt(listing: Listing) -> str:
    return f"Score this NYC apartment listing:\n\n{_listing_block(listing)}"


def _batch_prompt(listings: list[Listing]) -> str:
    blocks = "\n\n---\n\n".join(
        f"LISTING {i + 1} (id={l.id}):\n{_listing_block(l)}"
        for i, l in enumerate(listings)
    )
    return (
        "Score each NYC apartment listing below. "
        "Respond with a JSON array — one object per listing, in order — "
        "each with: score, summary, red_flags, highlights.\n\n"
        + blocks
    )


# ---------------------------------------------------------------------------
# JSON extraction (handles markdown code fences)
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Any:
    # Strip ```json ... ``` fences if present
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    raw = fenced.group(1) if fenced else text.strip()
    return json.loads(raw)


def _safe_result(raw: Any, listing_id: str) -> dict:
    """Normalise a single score object; fill defaults on bad data."""
    if not isinstance(raw, dict):
        log.warning("Unexpected score shape for %s: %r", listing_id, raw)
        return {"score": 0, "summary": "Parse error", "red_flags": [], "highlights": []}
    return {
        "score": int(raw.get("score", 0)),
        "summary": str(raw.get("summary", "")),
        "red_flags": list(raw.get("red_flags") or []),
        "highlights": list(raw.get("highlights") or []),
    }


# ---------------------------------------------------------------------------
# Cache check
# ---------------------------------------------------------------------------

def _cached_score(db: sqlite_utils.Database, listing_id: str) -> dict | None:
    rows = list(db["listings"].rows_where("id = ? AND score > 0", [listing_id]))
    if not rows:
        return None
    row = rows[0]
    # We only store the numeric score in the DB; return a minimal dict so the
    # caller can skip re-scoring without losing the full result.
    return {"score": row["score"], "summary": "", "red_flags": [], "highlights": [], "_from_cache": True}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_listings(
    listings: list[Listing],
    scoring_config: dict,
    db: sqlite_utils.Database | None = None,
) -> list[dict]:
    """
    Score a list of listings. Returns results in the same order as input.
    Uses batch mode when len(listings) > _BATCH_THRESHOLD.
    Skips listings that already have a cached score in `db`.
    """
    if not listings:
        return []

    model = scoring_config.get("model", "claude-sonnet-4-20250514")
    client = anthropic.Anthropic()

    # Partition into cached vs needs scoring
    results: dict[str, dict] = {}
    to_score: list[Listing] = []

    for listing in listings:
        if db is not None:
            cached = _cached_score(db, listing.id)
            if cached:
                log.debug("Score cache hit for %s", listing.id)
                results[listing.id] = cached
                continue
        to_score.append(listing)

    log.info("Scoring %d listing(s) via Claude (%d from cache)", len(to_score), len(results))

    if to_score:
        if len(to_score) > _BATCH_THRESHOLD:
            scored = _score_batch(client, model, to_score)
        else:
            scored = _score_individual(client, model, to_score)
        results.update(scored)

    # Return in original input order
    return [results.get(l.id, _safe_result({}, l.id)) for l in listings]


def score_listing(
    listing: Listing,
    scoring_config: dict,
    db: sqlite_utils.Database | None = None,
) -> dict:
    """Convenience wrapper for a single listing."""
    return score_listings([listing], scoring_config, db=db)[0]


# ---------------------------------------------------------------------------
# Individual scoring (≤ _BATCH_THRESHOLD listings)
# ---------------------------------------------------------------------------

def _score_individual(
    client: anthropic.Anthropic,
    model: str,
    listings: list[Listing],
) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for listing in listings:
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=512,
                system=_SYSTEM,
                messages=[{"role": "user", "content": _single_prompt(listing)}],
            )
            raw = _extract_json(msg.content[0].text)
            results[listing.id] = _safe_result(raw, listing.id)
            log.info("Scored %s → %s/10", listing.id, results[listing.id]["score"])
        except Exception as exc:
            log.warning("Score failed for %s: %s", listing.id, exc)
            results[listing.id] = _safe_result({}, listing.id)
        time.sleep(_RATE_DELAY)
    return results


# ---------------------------------------------------------------------------
# Batch scoring (> _BATCH_THRESHOLD listings)
# ---------------------------------------------------------------------------

def _score_batch(
    client: anthropic.Anthropic,
    model: str,
    listings: list[Listing],
) -> dict[str, dict]:
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=256 * len(listings),
            system=_SYSTEM,
            messages=[{"role": "user", "content": _batch_prompt(listings)}],
        )
        raw = _extract_json(msg.content[0].text)

        if not isinstance(raw, list):
            log.warning("Batch response was not a JSON array — falling back to individual scoring")
            return _score_individual(client, model, listings)

        if len(raw) != len(listings):
            log.warning(
                "Batch returned %d results for %d listings — falling back to individual scoring",
                len(raw), len(listings),
            )
            return _score_individual(client, model, listings)

        results: dict[str, dict] = {}
        for listing, item in zip(listings, raw):
            results[listing.id] = _safe_result(item, listing.id)
            log.info("Batch scored %s → %s/10", listing.id, results[listing.id]["score"])
        return results

    except Exception as exc:
        log.warning("Batch scoring failed (%s) — falling back to individual scoring", exc)
        return _score_individual(client, model, listings)
