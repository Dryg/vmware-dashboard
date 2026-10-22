"""vmdash-collector: sync vCenter into SQLite every SYNC_INTERVAL_MINUTES."""

import argparse
import logging
import signal
import sqlite3
import threading
import time
from collections.abc import Callable

from pydantic import ValidationError

from vmdash.collector.sync import run_sync
from vmdash.config import CollectorSettings, add_common_args, load_settings
from vmdash.db import connect_rw, fail_stale_runs
from vmdash.log import setup_logging

log = logging.getLogger(__name__)


def run_forever(settings: CollectorSettings, conn: sqlite3.Connection | None, stop: threading.Event,
                sync: Callable[[CollectorSettings, sqlite3.Connection | None], str] = run_sync) -> None:
    """Sync, then wait until the next interval measured from the start of the previous sync."""
    interval = settings.sync_interval_minutes * 60
    while not stop.is_set():
        started = time.monotonic()
        sync(settings, conn)
        if stop.wait(max(0.0, interval - (time.monotonic() - started))):
            break


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vmdash-collector", description="Sync vCenter inventory into SQLite.")
    add_common_args(parser)
    parser.add_argument("--once", action="store_true", help="run one sync and exit (1 if it failed)")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(CollectorSettings, args)
    except ValidationError as e:
        setup_logging("INFO", None)
        fields = ", ".join(".".join(map(str, err["loc"])).upper() for err in e.errors())
        log.error("invalid collector settings (check .env): %s", fields)
        return 2

    setup_logging(settings.log_level, settings.log_file)
    conn = connect_rw(settings.sqlite_path)
    try:
        stale = fail_stale_runs(conn)
        if stale:
            log.warning("marked %d interrupted sync run(s) as failed", stale)
        if args.once:
            return 1 if run_sync(settings, conn) == "failed" else 0

        stop = threading.Event()

        def on_signal(signum, _frame):
            log.info("received %s, stopping after the current step", signal.Signals(signum).name)
            stop.set()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        log.info("collector started: %s every %d min -> %s", settings.vcenter_host,
                 settings.sync_interval_minutes, settings.sqlite_path)
        run_forever(settings, conn, stop)
        log.info("collector stopped")
        return 0
    finally:
        conn.close()
