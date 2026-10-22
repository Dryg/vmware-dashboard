"""One sync run: fetch from vCenter, write the snapshot, record the run. Never raises."""

import logging
import sqlite3
import time
from collections.abc import Callable

from pyVmomi import vmodl

from vmdash.collector.tags import fetch_vm_tags
from vmdash.collector.transform import RawInventory, build_inventory, keep_last_network
from vmdash.collector.vcenter import connect, disconnect, fetch_raw
from vmdash.config import CollectorSettings, vcenter_tls
from vmdash.db import finish_run, read_inventory, read_snapshot_time, start_run, write_snapshot
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
                         settings.vcenter_password.get_secret_value(), vcenter_tls(settings) or False)


def _error(e: BaseException, settings: CollectorSettings) -> str:
    if isinstance(e, vmodl.MethodFault):  # str() of a vSphere fault is a multi-line object dump
        text = f"{type(e).__name__}: {e.msg or ''}"
    else:
        text = f"{type(e).__name__}: {e}"
    text = " ".join(text.split())
    password = settings.vcenter_password.get_secret_value()
    if password:
        text = text.replace(password, "***")
    return text[:MAX_ERROR]


def run_sync(settings: CollectorSettings, conn: sqlite3.Connection,
             fetch: Callable[[CollectorSettings], RawInventory] = fetch_inventory,
             fetch_tags: Callable[[CollectorSettings], dict[str, list[Tag]]] = fetch_tags_for) -> str:
    started = time.monotonic()
    try:
        run_id = start_run(conn)
    except sqlite3.Error as e:  # disk full, database locked: skip this run, the daemon keeps going
        log.error("sync not started, cannot write %s: %s", settings.sqlite_path, _error(e, settings))
        return "failed"
    log.info("sync started (run %d)", run_id)
    try:
        raw = fetch(settings)
    except Exception as e:  # noqa: BLE001 - any failure is recorded, the daemon keeps running
        return _fail(conn, run_id, e, settings)

    status, note, tags = "success", None, None
    try:
        tags = fetch_tags(settings)
        log.info("tags: %d VMs tagged", len(tags))
    except Exception as e:  # noqa: BLE001 - tags are optional; the VM data is still written
        status, note = "partial", f"tags: {_error(e, settings)}"[:MAX_ERROR]
        log.warning("tags failed, keeping the previous tags: %s", note)

    try:
        previous = {vm.moref: vm for vm in read_inventory(conn).vms}
        if tags is None:
            tags = {moref: list(vm.tags) for moref, vm in previous.items() if vm.tags}
        inv = build_inventory(raw, tags)
        inv = keep_last_network(inv, previous, read_snapshot_time(conn))
        write_snapshot(conn, inv, run_id, settings.sync_interval_minutes)
    except Exception as e:  # noqa: BLE001
        return _fail(conn, run_id, e, settings)

    _finish(conn, run_id, status, error=note, host_count=len(inv.hosts), vm_count=len(inv.vms))
    log.info("sync %s in %.1fs: %d hosts, %d VMs", status, time.monotonic() - started, len(inv.hosts), len(inv.vms))
    return status


def _finish(conn: sqlite3.Connection, run_id: int, status: str, **kw) -> None:
    try:
        finish_run(conn, run_id, status, **kw)
    except sqlite3.Error as e:  # the run stays "running" until fail_stale_runs at the next start
        log.error("could not record the end of run %d: %s", run_id, e)


def _fail(conn: sqlite3.Connection, run_id: int, e: Exception, settings: CollectorSettings) -> str:
    error = _error(e, settings)
    log.error("sync failed: %s", error)
    log.debug("sync failure details", exc_info=True)
    _finish(conn, run_id, "failed", error=error)
    return "failed"
