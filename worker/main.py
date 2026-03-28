"""Worker service: GitHub polling loop.

Runs on a configurable interval using APScheduler.
Local-first: no queue service, no Redis, no broker.
Just a plain blocking scheduler that fires on a cron.
"""

from __future__ import annotations

import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler

from franktheunicorn.config import get_settings, load_project_configs
from franktheunicorn.database import create_all_tables
from franktheunicorn.poller import run_poll_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _get_poll_interval() -> int:
    """Return the shortest poll interval across all configured projects."""
    settings = get_settings()
    projects = load_project_configs()
    enabled = [p for p in projects if p.enabled]
    if not enabled:
        return settings.default_poll_interval_seconds
    return min(p.poll_interval_seconds for p in enabled)


def run_once() -> None:
    """Run a single poll cycle (used for one-shot invocations and testing)."""
    logger.info("Running single poll cycle …")
    total = run_poll_cycle()
    logger.info("Single poll cycle complete: %d PRs processed", total)


def main() -> None:
    """Start the worker with a blocking scheduler."""
    logger.info("franktheunicorn worker starting …")
    create_all_tables()

    # Run immediately on startup so we don't wait for the first interval.
    try:
        run_poll_cycle()
    except Exception:
        logger.exception("Error in initial poll cycle (continuing)")

    interval = _get_poll_interval()
    logger.info("Scheduling poll every %d seconds", interval)

    scheduler = BlockingScheduler()
    scheduler.add_job(run_poll_cycle, "interval", seconds=interval, id="poll_cycle")

    def _handle_sigterm(signum: int, frame: object) -> None:
        logger.info("Received SIGTERM, shutting down …")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Worker stopped by keyboard interrupt")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
