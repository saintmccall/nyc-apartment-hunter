import argparse
import asyncio
import logging
import logging.handlers
from datetime import datetime, time as dtime, timezone
from pathlib import Path

from src.config import load_config
from src.scraper import scrape_source, Listing
from src.dedup import get_db, deduplicate, update_score, mark_notified, expire_old, get_stats
from src.scorer import score_listings
from src.notifier import notify, flush_digest
from src.monitor import check_health, send_weekly_summary

_LOG_DIR = Path("data")
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"

# Track whether the 8am digest and weekly summary have fired today
_digest_sent_date: str = ""
_weekly_sent_week: str = ""


def _setup_logging() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "hunter.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(file_handler)


_setup_logging()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------

def _in_quiet_hours(quiet_hours: str) -> bool:
    start_str, end_str = quiet_hours.split("-")
    now = datetime.now().time()
    start = dtime.fromisoformat(start_str)
    end = dtime.fromisoformat(end_str)
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


# ---------------------------------------------------------------------------
# Scheduled triggers
# ---------------------------------------------------------------------------

def _should_send_digest(email_cfg: dict) -> bool:
    global _digest_sent_date
    if not email_cfg.get("digest", False):
        return False
    today = datetime.now().date().isoformat()
    return _digest_sent_date != today and datetime.now().time() >= dtime(8, 0)


def _should_send_weekly() -> bool:
    global _weekly_sent_week
    # ISO week string e.g. "2026-W21"
    week = datetime.now().isocalendar()
    week_key = f"{week.year}-W{week.week:02d}"
    # Send on Monday (weekday 1) after 8am
    return (
        _weekly_sent_week != week_key
        and datetime.now().weekday() == 0
        and datetime.now().time() >= dtime(8, 0)
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(config: dict) -> None:
    search = config["search"]
    sources = [s for s in config["sources"] if s.get("enabled")]
    sources.sort(key=lambda s: s.get("priority", 99))
    email_cfg = config["notifications"]["email"]

    db = get_db()
    expire_old(db)

    # 8am digest flush
    global _digest_sent_date
    if _should_send_digest(email_cfg):
        try:
            flush_digest(db, email_cfg)
            _digest_sent_date = datetime.now().date().isoformat()
        except Exception as exc:
            log.error("Digest flush failed: %s", exc)

    # Monday weekly summary
    global _weekly_sent_week
    if _should_send_weekly():
        try:
            sent = send_weekly_summary(db, email_cfg)
            if sent:
                week = datetime.now().isocalendar()
                _weekly_sent_week = f"{week.year}-W{week.week:02d}"
        except Exception as exc:
            log.error("Weekly summary failed: %s", exc)

    # Scrape
    all_listings: list[Listing] = []
    for source in sources:
        log.info("Scraping %s…", source["name"])
        try:
            listings = await scrape_source(source, search)
            all_listings.extend(listings)
        except Exception as exc:
            log.warning("Failed to scrape %s: %s", source["name"], exc)

    # Dedup
    new_listings = deduplicate(db, all_listings)

    # Score
    scored_results = score_listings(new_listings, config["scoring"], db=db)
    results: list[dict] = []
    for listing, result in zip(new_listings, scored_results):
        update_score(db, listing.id, result.get("score", 0))
        log.info("[%s] %s — score %s/10", listing.source, listing.title, result.get("score"))
        results.append({"listing": listing, "result": result})

    # Notify
    if results:
        notified = notify(results, config, db=db)
        for item in notified:
            mark_notified(db, item["listing"].id)

    # Health check (after scraping so a fresh empty run triggers it)
    check_health(db, email_cfg)

    stats = get_stats(db)
    log.info(
        "Stats — total: %d | new today: %d | avg score: %.1f",
        stats["total_seen"], stats["new_today"], stats["avg_score"],
    )


async def loop(config: dict) -> None:
    interval = config["schedule"]["interval_minutes"] * 60
    quiet = config["schedule"].get("quiet_hours", "")

    while True:
        if quiet and _in_quiet_hours(quiet):
            log.info("Quiet hours active — skipping run")
        else:
            await run_pipeline(config)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_recent(args: argparse.Namespace, config: dict) -> None:
    db = get_db()
    rows = list(
        db.execute(
            "SELECT source, title, neighborhood, price, score, url, first_seen "
            "FROM listings ORDER BY first_seen DESC LIMIT ?",
            [args.recent],
        )
    )
    if not rows:
        print("No listings in database yet.")
        return

    print(f"\n{'#':<4} {'Score':<7} {'Price':<9} {'Neighborhood':<20} {'Source':<12} Title")
    print("─" * 90)
    for i, (source, title, hood, price, score, url, seen) in enumerate(rows, 1):
        score_str = f"{score:.0f}/10" if score else "  —  "
        title_short = (title or url)[:40]
        print(f"{i:<4} {score_str:<7} ${price:<8,} {hood:<20} {source:<12} {title_short}")
    print()


def cmd_set(args: argparse.Namespace, config: dict) -> None:
    import yaml
    key, value = args.set

    # Try to coerce the value to int or float
    coerced: int | float | str
    try:
        coerced = int(value)
    except ValueError:
        try:
            coerced = float(value)
        except ValueError:
            coerced = value

    # Allow dotted keys like "search.price_max" or bare keys matched under "search"
    if "." in key:
        parts = key.split(".", 1)
        section, field = parts[0], parts[1]
        if section not in config:
            print(f"Unknown section: {section}")
            return
        config[section][field] = coerced
    else:
        # Search the most common sections
        for section in ("search", "schedule", "scoring"):
            if key in config.get(section, {}):
                config[section][key] = coerced
                print(f"Set {section}.{key} = {coerced!r}")
                break
        else:
            print(f"Key '{key}' not found in search/schedule/scoring sections.")
            return

    with open("config.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"config.yaml updated.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="NYC Apartment Hunter")
    parser.add_argument("--recent", type=int, metavar="N",
                        help="Print the N most recent listings and exit")
    parser.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"),
                        help="Set a config value (e.g. --set price_max 5000) and exit")
    args = parser.parse_args()

    config = load_config()

    if args.recent:
        cmd_recent(args, config)
        return

    if args.set:
        cmd_set(args, config)
        return

    asyncio.run(loop(config))


if __name__ == "__main__":
    main()
