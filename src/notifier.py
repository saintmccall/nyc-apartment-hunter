from __future__ import annotations

import json
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import sqlite_utils
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .scraper import Listing

log = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_DIGEST_TABLE = "digest_queue"

# ---------------------------------------------------------------------------
# Jinja2 env
# ---------------------------------------------------------------------------

_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _render(items: list[dict]) -> str:
    items_sorted = sorted(items, key=lambda x: x["result"].get("score", 0), reverse=True)
    template = _jinja.get_template("email.html")
    return template.render(
        listings=items_sorted,
        date=datetime.now(timezone.utc).strftime("%B %-d, %Y"),
    )


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------

def _send(html: str, subject: str, email_config: dict) -> None:
    address = email_config["address"]
    app_password = email_config.get("app_password", "")
    smtp_host = email_config.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(email_config.get("smtp_port", 587))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = address
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            if app_password:
                server.login(address, app_password)
            server.sendmail(address, [address], msg.as_string())
        log.info("Email sent to %s (%s)", address, subject)
    except OSError as exc:
        log.error("Email send failed (SMTP unreachable — check DO firewall): %s", exc)


# ---------------------------------------------------------------------------
# Digest queue (SQLite-backed)
# ---------------------------------------------------------------------------

def _ensure_digest_table(db: sqlite_utils.Database) -> None:
    if _DIGEST_TABLE not in db.table_names():
        db[_DIGEST_TABLE].create({
            "id": int,
            "queued_at": str,
            "payload": str,   # JSON blob of one {listing, result} item
        }, pk="id")


def queue_for_digest(db: sqlite_utils.Database, items: list[dict]) -> None:
    _ensure_digest_table(db)
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        db[_DIGEST_TABLE].insert({
            "queued_at": now,
            "payload": json.dumps({
                "listing": {
                    "id": item["listing"].id,
                    "source": item["listing"].source,
                    "url": item["listing"].url,
                    "title": item["listing"].title,
                    "price": item["listing"].price,
                    "bedrooms": item["listing"].bedrooms,
                    "neighborhood": item["listing"].neighborhood,
                    "address": item["listing"].address,
                    "description": item["listing"].description,
                    "has_broker_fee": item["listing"].has_broker_fee,
                },
                "result": item["result"],
            }),
        })
    log.info("Queued %d item(s) for digest", len(items))


def flush_digest(db: sqlite_utils.Database, email_config: dict) -> int:
    """Send one digest email with everything in the queue, then clear it. Returns count sent."""
    _ensure_digest_table(db)
    rows = list(db[_DIGEST_TABLE].rows)
    if not rows:
        log.info("Digest flush: nothing queued")
        return 0

    items: list[dict] = []
    for row in rows:
        data = json.loads(row["payload"])
        raw = data["listing"]
        listing = Listing(
            id=raw["id"],
            source=raw["source"],
            url=raw["url"],
            title=raw["title"],
            price=raw["price"],
            bedrooms=raw["bedrooms"],
            neighborhood=raw["neighborhood"],
            address=raw["address"],
            description=raw["description"],
            has_broker_fee=raw.get("has_broker_fee"),
        )
        items.append({"listing": listing, "result": data["result"]})

    html = _render(items)
    subject = f"NYC Hunter digest — {len(items)} listing(s) · {datetime.now(timezone.utc).strftime('%b %-d')}"
    _send(html, subject, email_config)

    db[_DIGEST_TABLE].delete_where()
    log.info("Digest sent and queue cleared (%d items)", len(items))
    return len(items)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify(
    listings_with_scores: list[dict],
    config: dict,
    db: sqlite_utils.Database | None = None,
) -> list[dict]:
    """
    Send or queue notifications for qualified listings (score >= min_score).

    Modes (set in config.notifications.email):
      digest: true  — append to digest queue; flush_digest() sends at 8am
      digest: false — send immediately (one email per run)

    Returns the list of items that qualified (score >= min_score).
    """
    email_cfg = config["notifications"]["email"]
    if not email_cfg.get("enabled", False):
        return []

    min_score = email_cfg.get("min_score", 6)
    qualified = [
        item for item in listings_with_scores
        if item["result"].get("score", 0) >= min_score
    ]
    if not qualified:
        log.info("No listings met the min_score threshold (%d)", min_score)
        return []

    if email_cfg.get("digest", False):
        if db is None:
            log.warning("Digest mode requested but no DB passed — sending immediately instead")
            _send_immediate(qualified, email_cfg)
        else:
            queue_for_digest(db, qualified)
    else:
        _send_immediate(qualified, email_cfg)

    return qualified


def _send_immediate(items: list[dict], email_cfg: dict) -> None:
    html = _render(items)
    subject = f"NYC Hunter: {len(items)} new match{'es' if len(items) != 1 else ''}"
    _send(html, subject, email_cfg)
