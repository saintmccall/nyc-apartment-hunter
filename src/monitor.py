from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import sqlite_utils

from .notifier import _send, _render
from .scraper import Listing

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Health check — alert if no new listings seen in 24 hours
# ---------------------------------------------------------------------------

def check_health(db: sqlite_utils.Database, email_config: dict) -> bool:
    """
    Send an alert email if no listing has been seen in the last 24 hours.
    Returns True if healthy, False if alert was sent.
    """
    if not email_config.get("enabled"):
        return True

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    recent = db["listings"].count_where("first_seen >= ?", [cutoff])

    if recent > 0:
        return True

    log.warning("Health check FAILED — no new listings in 24 hours")
    _send(
        html=_health_alert_html(),
        subject="NYC Hunter ⚠ No new listings in 24 hours",
        email_config=email_config,
    )
    return False


def _health_alert_html() -> str:
    return f"""\
<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;max-width:480px;margin:40px auto;color:#1a1a2e;">
  <h2 style="color:#b91c1c;">⚠ NYC Hunter Health Alert</h2>
  <p>No new apartment listings have been found in the last <strong>24 hours</strong>.</p>
  <p>This may indicate:</p>
  <ul>
    <li>Scraper selectors are outdated (site HTML changed)</li>
    <li>The process is not running / launchd agent stopped</li>
    <li>Rate limiting or IP block by a source</li>
  </ul>
  <p style="color:#6b7280;font-size:13px;">Sent at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Weekly summary
# ---------------------------------------------------------------------------

def send_weekly_summary(db: sqlite_utils.Database, email_config: dict) -> bool:
    """
    Email a weekly digest: total scanned, avg score, top 3 listings.
    Returns True if email was sent.
    """
    if not email_config.get("enabled"):
        return False

    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    total = db["listings"].count_where("first_seen >= ?", [since])
    if total == 0:
        log.info("Weekly summary: no listings this week, skipping")
        return False

    row = next(
        db.execute(
            "SELECT AVG(score) FROM listings WHERE first_seen >= ? AND score > 0",
            [since],
        ),
        (None,),
    )
    avg_score = round(float(row[0]), 1) if row[0] else 0.0

    top_rows = list(
        db.execute(
            "SELECT id, url, title, price, neighborhood, score, source "
            "FROM listings WHERE first_seen >= ? AND score > 0 "
            "ORDER BY score DESC LIMIT 3",
            [since],
        )
    )

    top_items = [
        {
            "listing": Listing(
                id=r[0], source=r[6], url=r[1], title=r[2], price=r[3],
                bedrooms=1.0, neighborhood=r[4], address="", description="",
            ),
            "result": {
                "score": r[5],
                "summary": "",
                "red_flags": [],
                "highlights": [],
            },
        }
        for r in top_rows
    ]

    html = _weekly_html(total, avg_score, top_items, since)
    subject = f"NYC Hunter weekly — {total} listings scanned · avg {avg_score}/10"
    _send(html, subject, email_config)
    log.info("Weekly summary sent (%d listings, avg %.1f)", total, avg_score)
    return True


def _weekly_html(total: int, avg_score: float, top_items: list[dict], since: str) -> str:
    since_date = since[:10]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    top_rows_html = ""
    for rank, item in enumerate(top_items, 1):
        l = item["listing"]
        score = item["result"]["score"]
        top_rows_html += f"""
        <tr>
          <td style="padding:8px 12px;font-weight:600;">{rank}</td>
          <td style="padding:8px 12px;"><a href="{l.url}" style="color:#1a1a2e;">{l.title or l.address or l.url}</a></td>
          <td style="padding:8px 12px;">{l.neighborhood}</td>
          <td style="padding:8px 12px;">${l.price:,}</td>
          <td style="padding:8px 12px;font-weight:700;">{int(score)}/10</td>
        </tr>"""

    return f"""\
<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;max-width:600px;margin:40px auto;color:#1a1a2e;">
  <h2>NYC Hunter — Weekly Summary</h2>
  <p style="color:#6b7280;">{since_date} → {today}</p>

  <table style="border-collapse:collapse;width:100%;margin:20px 0;">
    <tr>
      <td style="padding:16px;background:#f3f4f6;border-radius:8px;text-align:center;width:33%;">
        <div style="font-size:28px;font-weight:700;">{total}</div>
        <div style="font-size:13px;color:#6b7280;">Listings scanned</div>
      </td>
      <td style="width:16px;"></td>
      <td style="padding:16px;background:#f3f4f6;border-radius:8px;text-align:center;width:33%;">
        <div style="font-size:28px;font-weight:700;">{avg_score}</div>
        <div style="font-size:13px;color:#6b7280;">Avg score / 10</div>
      </td>
    </tr>
  </table>

  <h3>Top 3 listings this week</h3>
  <table style="border-collapse:collapse;width:100%;">
    <thead>
      <tr style="background:#1a1a2e;color:#fff;">
        <th style="padding:8px 12px;text-align:left;">#</th>
        <th style="padding:8px 12px;text-align:left;">Listing</th>
        <th style="padding:8px 12px;text-align:left;">Neighborhood</th>
        <th style="padding:8px 12px;text-align:left;">Price</th>
        <th style="padding:8px 12px;text-align:left;">Score</th>
      </tr>
    </thead>
    <tbody>{top_rows_html}</tbody>
  </table>

  <p style="color:#9ca3af;font-size:12px;margin-top:32px;">NYC Apartment Hunter</p>
</body>
</html>"""
