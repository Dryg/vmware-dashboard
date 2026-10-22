"""Read the snapshot and sync status from SQLite."""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from vmdash.models import Datastore, Disk, Host, Inventory, Tag, Vm

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


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def read_inventory(conn: sqlite3.Connection) -> Inventory:
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    disks: dict[str, list[Disk]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM disks ORDER BY rowid"):
        disks[r["vm_moref"]].append(Disk(r["key"], r["label"], r["capacity_bytes"], r["used_bytes"], r["datastore"]))
    tags: dict[str, list[Tag]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM tags ORDER BY rowid"):
        tags[r["vm_moref"]].append(Tag(r["category"], r["name"]))
    hosts = tuple(
        Host(r["moref"], r["name"], r["cluster"], r["connection_state"], bool(r["in_maintenance"]))
        for r in conn.execute("SELECT * FROM hosts ORDER BY rowid")
    )
    datastores = tuple(
        Datastore(r["moref"], r["name"], r["capacity_bytes"], r["free_bytes"])
        for r in conn.execute("SELECT * FROM datastores ORDER BY rowid")
    )
    vms = tuple(
        Vm(
            moref=r["moref"], name=r["name"], host_moref=r["host_moref"], description=r["description"],
            folder_path=r["folder_path"], power_state=r["power_state"], connection_state=r["connection_state"],
            is_template=bool(r["is_template"]), vcpu=r["vcpu"], memory_mb=r["memory_mb"], guest_os=r["guest_os"],
            boot_time=_dt(r["boot_time"]), datastore_path=r["datastore_path"],
            disks=tuple(disks.get(r["moref"], ())), tags=tuple(tags.get(r["moref"], ())),
        )
        for r in conn.execute("SELECT * FROM vms ORDER BY rowid")
    )
    return Inventory(meta.get("vcenter_host", ""), meta.get("datacenter", ""), hosts, datastores, vms)


def read_status(conn: sqlite3.Connection) -> SyncStatus:
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    r = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    last_run = RunInfo(r["id"], r["status"], _dt(r["started_at"]), _dt(r["finished_at"]), r["error"]) if r else None
    ok = conn.execute(
        "SELECT finished_at FROM sync_runs WHERE status IN ('success', 'partial') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    snapshot = meta.get("snapshot_id")
    return SyncStatus(
        snapshot_id=int(snapshot) if snapshot else None,
        last_run=last_run,
        last_success_at=_dt(ok[0]) if ok else None,
        interval_minutes=int(meta.get("sync_interval_minutes", DEFAULT_INTERVAL)),
        vcenter_host=meta.get("vcenter_host", ""),
        datacenter=meta.get("datacenter", ""),
        snapshot_uid=meta.get("snapshot_uid"),
    )
