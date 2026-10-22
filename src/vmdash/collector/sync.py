"""One sync run: fetch from vCenter, write the snapshot, record the run. Never raises."""

import logging
import sqlite3
import time
from collections.abc import Callable

from vmdash.collector.tags import fetch_vm_tags
from vmdash.collector.transform import RawInventory, build_inventory
from vmdash.collector.vcenter import connect, disconnect, fetch_raw
from vmdash.config import CollectorSettings
from vmdash.db import finish_run, start_run, write_snapshot
from vmdash.models import Tag

log = logging.getLogger(__name__)

MAX_ERROR = 500


def fetch_inventory(settings: CollectorSettings) -> RawInventory:
    si = connect(settings)
    try:
        return fetch_raw(si, settings)
    finally:
        disconnect(si)


def fetch_tags_for(settings: CollectorSettings) -> dict[str, list[Tag]]:
    return fetch_vm_tags(settings.vcenter_host, settings.vcenter_user,
                         settings.vcenter_password.get_secret_value(), settings.vcenter_verify_ssl)


def _error(e: BaseException, settings: CollectorSettings) -> str:
    text = f"{type(e).__name__}: {e}".strip()
    password = settings.vcenter_password.get_secret_value()
    if password:
        text = text.replace(password, "***")
    return text[:MAX_ERROR]


def run_sync(settings: CollectorSettings, conn: sqlite3.Connection,
             fetch: Callable[[CollectorSettings], RawInventory] = fetch_inventory,
             fetch_tags: Callable[[CollectorSettings], dict[str, list[Tag]]] = fetch_tags_for) -> str:
    started = time.monotonic()
    run_id = start_run(conn)
    log.info("sync started (run %d)", run_id)
    try:
        raw = fetch(settings)
    except Exception as e:  # noqa: BLE001 - any failure is recorded, the daemon keeps running
        return _fail(conn, run_id, e, settings)

    status, note = "success", None
    try:
        tags = fetch_tags(settings)
        log.info("tags: %d VMs tagged", len(tags))
    except Exception as e:  # noqa: BLE001 - tags are optional; the VM data is still written
        status, note, tags = "partial", f"tags: {_error(e, settings)}"[:MAX_ERROR], {}
        log.warning("tags failed: %s", note)

    try:
        inv = build_inventory(raw, tags)
        write_snapshot(conn, inv, run_id, settings.sync_interval_minutes)
    except Exception as e:  # noqa: BLE001
        return _fail(conn, run_id, e, settings)

    finish_run(conn, run_id, status, error=note, host_count=len(inv.hosts), vm_count=len(inv.vms))
    log.info("sync %s in %.1fs: %d hosts, %d VMs", status, time.monotonic() - started, len(inv.hosts), len(inv.vms))
    return status


def _fail(conn: sqlite3.Connection, run_id: int, e: Exception, settings: CollectorSettings) -> str:
    error = _error(e, settings)
    log.error("sync failed: %s", error)
    log.debug("sync failure details", exc_info=True)
    finish_run(conn, run_id, "failed", error=error)
    return "failed"
