"""Read the snapshot and sync status from SQLite."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from vmdash.db import parse_iso
from vmdash.models import Inventory

DEFAULT_INTERVAL = 15


@dataclass(frozen=True, slots=True)
class RunInfo:
    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SyncStatus:
    snapshot_id: int | None
    last_run: RunInfo | None
    last_success_at: datetime | None
    interval_minutes: int
    vcenter_host: str
    datacenter: str
    snapshot_uid: str | None = None  # unique per written snapshot, unlike ids which restart with a new DB


EMPTY_STATUS = SyncStatus(None, None, None, DEFAULT_INTERVAL, "", "")
EMPTY_INVENTORY = Inventory("", "", (), (), ())


def read_status(conn: sqlite3.Connection) -> SyncStatus:
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    r = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    last_run = RunInfo(r["id"], r["status"], parse_iso(r["started_at"]), parse_iso(r["finished_at"]),
                       r["error"]) if r else None
    ok = conn.execute(
        "SELECT finished_at FROM sync_runs WHERE status IN ('success', 'partial') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    snapshot = meta.get("snapshot_id")
    return SyncStatus(
        snapshot_id=int(snapshot) if snapshot else None,
        last_run=last_run,
        # runs are pruned to the last 500, so after a long outage the snapshot time is the last success
        last_success_at=parse_iso(ok[0]) if ok else parse_iso(meta.get("snapshot_at")),
        interval_minutes=int(meta.get("sync_interval_minutes", DEFAULT_INTERVAL)),
        vcenter_host=meta.get("vcenter_host", ""),
        datacenter=meta.get("datacenter", ""),
        snapshot_uid=meta.get("snapshot_uid"),
    )
