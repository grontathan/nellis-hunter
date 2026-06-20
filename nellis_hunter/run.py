"""Part C — CLI entry point for the scheduled job: `python -m nellis_hunter.run`.

One full Phoenix+Mesa sweep across the configured flip categories, scored, deduped,
ranked, and pushed to the configured sink (Discord, else console). No internal
scheduler loop — schedule this command externally (see README: GitHub Actions cron,
Windows Task Scheduler, or unix cron).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from .config import load_config
from .db import NellisDB
from .notify import ConsoleNotifier, InteractionNotifier, build_notifier
from .pipeline import Pipeline


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252 and choke on emoji/typographic chars.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        prog="python -m nellis_hunter.run",
        description="Sweep Nellis (Phoenix+Mesa), score for flip potential, post a ranked digest.",
    )
    ap.add_argument("--locations", default=None, help="Pipe-separated, e.g. 'Phoenix|Mesa'")
    ap.add_argument("--categories", default=None, help="Pipe-separated taxonomy values; default from .env")
    ap.add_argument("--keywords", default=None, help="Optional free-text filter")
    ap.add_argument("--closing-within-hours", type=float, default=None)
    ap.add_argument("--max-details", type=int, default=None, help="Cap live-bid fetches")
    ap.add_argument("--console", action="store_true", help="Force console output (ignore webhook)")
    ap.add_argument("--no-db", action="store_true", help="Skip persistence/dedup (one-shot)")
    ap.add_argument("--dry-run", action="store_true", help="Score + print but do not send to webhook")
    args = ap.parse_args(argv)

    config = load_config()
    locations = args.locations.split("|") if args.locations else None
    categories = args.categories.split("|") if args.categories else None

    pipeline = Pipeline(config)
    db = None if args.no_db else NellisDB(config.db_path)
    try:
        result = pipeline.sweep(
            locations=locations,
            categories=categories,
            keywords=args.keywords,
            closing_within_hours=args.closing_within_hours,
            max_detail_fetches=args.max_details,
            db=db,
        )
    except Exception as exc:  # surface, exit non-zero so a scheduler flags the failure
        print(f"[nellis-hunter] sweep failed: {exc}", file=sys.stderr)
        pipeline.close()
        if db:
            db.close()
        return 1

    now = datetime.now(timezone.utc).astimezone()
    header = (
        f"**Nellis Hunter — {now:%a %b %d, %I:%M%p}** · "
        f"{len(result.surfaced)} surfaced / {result.candidates_seen} scanned "
        f"({result.detail_fetches} live-bid checks)"
    )

    # Three sinks, in priority order:
    #   1. console/dry-run  -> stdout (local debugging, CI dry checks)
    #   2. interaction env  -> post back to a Discord /hunt slash command
    #   3. default          -> the configured webhook (scheduled digest)
    app_id = os.getenv("INTERACTION_APP_ID")
    token = os.getenv("INTERACTION_TOKEN")
    if args.console or args.dry_run:
        notifier = ConsoleNotifier()
    elif app_id and token:
        notifier = InteractionNotifier(app_id, token)
    else:
        notifier = build_notifier(config.discord_webhook_url)
    notifier.send(result.surfaced, header=header)

    pipeline.close()
    if db:
        db.close()
    if hasattr(notifier, "close"):
        notifier.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
