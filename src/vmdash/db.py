"""SQLite schema, connections, writes and the inventory reader. The collector is the only writer."""

import logging
import sqlite3
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from vmdash.models import Datastore, Disk, Host, Inventory, Nic, Tag, Vm

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3
KEEP_RUNS = 500
SNAPSHOT_TABLES = ("hosts", "datastores", "vms", "disks", "nics", "tags", "meta")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL, error TEXT, host_count INTEGER NOT NULL DEFAULT 0,
    vm_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS hosts (
    moref TEXT PRIMARY KEY, name TEXT NOT NULL, cluster TEXT,
    connection_state TEXT NOT NULL, in_maintenance INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS datastores (
    moref TEXT PRIMARY KEY, name TEXT NOT NULL, capacity_bytes INTEGER NOT NULL, free_bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS vms (
    moref TEXT PRIMARY KEY, host_moref TEXT, name TEXT NOT NULL, description TEXT NOT NULL,
    folder_path TEXT NOT NULL, power_state TEXT NOT NULL, connection_state TEXT NOT NULL,
    is_template INTEGER NOT NULL, vcpu INTEGER NOT NULL, memory_mb INTEGER NOT NULL,
    guest_os TEXT NOT NULL, boot_time TEXT, datastore_path TEXT NOT NULL,
    dns_name TEXT NOT NULL, net_seen_at TEXT);
CREATE TABLE IF NOT EXISTS disks (
    vm_moref TEXT NOT NULL, key INTEGER NOT NULL, label TEXT NOT NULL,
    capacity_bytes INTEGER NOT NULL, used_bytes INTEGER NOT NULL, datastore TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nics (
    vm_moref TEXT NOT NULL, key INTEGER NOT NULL, label TEXT NOT NULL, network TEXT NOT NULL,
    mac TEXT NOT NULL, connected INTEGER NOT NULL, ips TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tags (vm_moref TEXT NOT NULL, category TEXT NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS disks_vm ON disks (vm_moref);
CREATE INDEX IF NOT EXISTS nics_vm ON nics (vm_moref);
CREATE INDEX IF NOT EXISTS tags_vm ON tags (vm_moref);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def connect_rw(path: Path) -> sqlite3.Connection:
    """Open for writing; create the schema, recreating it if the stored version is unknown."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        log.warning("schema version %s != %s, recreating database tables", version, SCHEMA_VERSION)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            conn.execute(f'DROP TABLE "{t}"')
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def connect_ro(path: Path) -> sqlite3.Connection | None:
    """Open read-only; None when the file is missing or has another schema version."""
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        log.warning("cannot open %s: %s", path, e)
        return None
    try:
        conn.row_factory = sqlite3.Row
        if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return conn
    except sqlite3.Error as e:
        log.warning("cannot read %s: %s", path, e)
    conn.close()
    return None


def start_run(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    cur = conn.execute("INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')", (_iso(_now(now)),))
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, error: str | None = None,
               host_count: int = 0, vm_count: int = 0, now: datetime | None = None) -> None:
    conn.execute(
        "UPDATE sync_runs SET status=?, error=?, host_count=?, vm_count=?, finished_at=? WHERE id=?",
        (status, error, host_count, vm_count, _iso(_now(now)), run_id),
    )
    conn.execute(
        "DELETE FROM sync_runs WHERE id NOT IN (SELECT id FROM sync_runs ORDER BY id DESC LIMIT ?)",
        (KEEP_RUNS,),
    )


def fail_stale_runs(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    cur = conn.execute(
        "UPDATE sync_runs SET status='failed', error='collector stopped during sync', finished_at=? "
        "WHERE status='running'", (_iso(_now(now)),))
    return cur.rowcount


def write_snapshot(conn: sqlite3.Connection, inv: Inventory, run_id: int, interval_minutes: int,
                   now: datetime | None = None) -> None:
    """Replace the whole snapshot in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for t in SNAPSHOT_TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.executemany(
            "INSERT INTO hosts VALUES (?, ?, ?, ?, ?)",
            [(h.moref, h.name, h.cluster, h.connection_state, int(h.in_maintenance)) for h in inv.hosts],
        )
        conn.executemany(
            "INSERT INTO datastores VALUES (?, ?, ?, ?)",
            [(d.moref, d.name, d.capacity_bytes, d.free_bytes) for d in inv.datastores],
        )
        conn.executemany(
            "INSERT INTO vms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(v.moref, v.host_moref, v.name, v.description, v.folder_path, v.power_state,
              v.connection_state, int(v.is_template), v.vcpu, v.memory_mb, v.guest_os,
              _iso(v.boot_time), v.datastore_path, v.dns_name, _iso(v.net_seen_at)) for v in inv.vms],
        )
        conn.executemany(
            "INSERT INTO disks VALUES (?, ?, ?, ?, ?, ?)",
            [(v.moref, d.key, d.label, d.capacity_bytes, d.used_bytes, d.datastore)
             for v in inv.vms for d in v.disks],
        )
        conn.executemany(
            "INSERT INTO nics VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(v.moref, n.key, n.label, n.network, n.mac, int(n.connected), " ".join(n.ips))
             for v in inv.vms for n in v.nics],
        )
        conn.executemany(
            "INSERT INTO tags VALUES (?, ?, ?)",
            [(v.moref, t.category, t.name) for v in inv.vms for t in v.tags],
        )
        conn.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [("vcenter_host", inv.vcenter_host), ("datacenter", inv.datacenter),
             ("sync_interval_minutes", str(interval_minutes)), ("snapshot_id", str(run_id)),
             ("snapshot_uid", uuid.uuid4().hex), ("snapshot_at", _iso(_now(now)))],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def read_inventory(conn: sqlite3.Connection) -> Inventory:
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    disks: dict[str, list[Disk]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM disks ORDER BY rowid"):
        disks[r["vm_moref"]].append(Disk(r["key"], r["label"], r["capacity_bytes"], r["used_bytes"], r["datastore"]))
    nics: dict[str, list[Nic]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM nics ORDER BY rowid"):
        nics[r["vm_moref"]].append(Nic(r["key"], r["label"], r["network"], r["mac"], bool(r["connected"]),
                                       tuple(r["ips"].split())))
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
            boot_time=parse_iso(r["boot_time"]), datastore_path=r["datastore_path"], dns_name=r["dns_name"],
            net_seen_at=parse_iso(r["net_seen_at"]),
            disks=tuple(disks.get(r["moref"], ())), nics=tuple(nics.get(r["moref"], ())),
            tags=tuple(tags.get(r["moref"], ())),
        )
        for r in conn.execute("SELECT * FROM vms ORDER BY rowid")
    )
    return Inventory(meta.get("vcenter_host", ""), meta.get("datacenter", ""), hosts, datastores, vms)


def read_snapshot_time(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT value FROM meta WHERE key='snapshot_at'").fetchone()
    return parse_iso(row[0]) if row else None
