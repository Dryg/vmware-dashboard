import sqlite3
from datetime import UTC, datetime

import pytest
from factories import make_inventory, make_vm

from vmdash.db import (
    connect_ro,
    connect_rw,
    fail_stale_runs,
    finish_run,
    start_run,
    write_snapshot,
)


@pytest.fixture
def conn(tmp_path):
    c = connect_rw(tmp_path / "a.db")
    yield c
    c.close()


def names(conn, table, col="name"):
    return sorted(r[0] for r in conn.execute(f"select {col} from {table}"))


def test_schema_created_with_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_creates_parent_dir(tmp_path):
    connect_rw(tmp_path / "sub" / "dir" / "a.db").close()
    assert (tmp_path / "sub" / "dir" / "a.db").exists()


def test_write_snapshot_replaces_previous(conn):
    rid = start_run(conn)
    write_snapshot(conn, make_inventory(vms=[make_vm("a"), make_vm("b")]), rid, 15)
    rid = start_run(conn)
    write_snapshot(conn, make_inventory(vms=[make_vm("c")]), rid, 15)
    assert names(conn, "vms") == ["c"]
    assert names(conn, "disks", "vm_moref") == ["vm-c"]
    assert names(conn, "tags", "vm_moref") == ["vm-c"]


def test_snapshot_meta(conn):
    rid = start_run(conn)
    write_snapshot(conn, make_inventory(), rid, 15)
    meta = dict(conn.execute("select key, value from meta").fetchall())
    assert len(meta.pop("snapshot_uid")) == 32
    assert meta == {"vcenter_host": "vcsa01.lab.local", "datacenter": "DC-Main",
                    "sync_interval_minutes": "15", "snapshot_id": str(rid)}


def test_boot_time_round_trip_is_utc_iso(conn):
    rid = start_run(conn)
    write_snapshot(conn, make_inventory(vms=[make_vm(boot_time=datetime(2026, 10, 1, 8, 0, tzinfo=UTC))]),
                   rid, 15)
    assert conn.execute("select boot_time from vms").fetchone()[0] == "2026-10-01T08:00:00+00:00"


def test_fail_stale_runs(conn):
    rid = start_run(conn)
    assert fail_stale_runs(conn) == 1
    row = conn.execute("select status, error from sync_runs where id=?", (rid,)).fetchone()
    assert tuple(row) == ("failed", "collector stopped during sync")


def test_finish_run_records_counts(conn):
    rid = start_run(conn, now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
    finish_run(conn, rid, "success", host_count=3, vm_count=7, now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))
    row = conn.execute("select * from sync_runs where id=?", (rid,)).fetchone()
    assert (row["status"], row["host_count"], row["vm_count"]) == ("success", 3, 7)
    assert row["started_at"] == "2026-10-22T12:00:00+00:00"
    assert row["finished_at"] == "2026-10-22T12:01:00+00:00"


def test_runs_pruned_to_500(conn):
    for _ in range(505):
        finish_run(conn, start_run(conn), "success")
    assert conn.execute("select count(*) from sync_runs").fetchone()[0] == 500
    assert conn.execute("select min(id) from sync_runs").fetchone()[0] == 6


def test_old_schema_version_is_recreated(tmp_path):
    path = tmp_path / "a.db"
    raw = sqlite3.connect(path)
    raw.execute("create table junk (x)")
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    conn = connect_rw(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = names(conn, "sqlite_master where type='table'")
    assert "junk" not in tables and "vms" in tables


def test_connect_ro_missing_file_returns_none(tmp_path):
    assert connect_ro(tmp_path / "nope.db") is None
    assert not (tmp_path / "nope.db").exists()


def test_connect_ro_wrong_version_returns_none(tmp_path):
    path = tmp_path / "a.db"
    sqlite3.connect(path).close()
    assert connect_ro(path) is None


def test_connect_ro_cannot_write(tmp_path):
    connect_rw(tmp_path / "a.db").close()
    ro = connect_ro(tmp_path / "a.db")
    assert ro is not None
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("insert into meta values ('a', 'b')")


def test_connect_ro_garbage_file_returns_none(tmp_path):
    (tmp_path / "junk.db").write_bytes(b"this is not sqlite" * 100)
    assert connect_ro(tmp_path / "junk.db") is None


def test_connect_ro_directory_returns_none(tmp_path):
    assert connect_ro(tmp_path) is None


def test_connect_ro_path_with_special_characters(tmp_path):
    path = tmp_path / "we#ird%dir" / "f.db"
    connect_rw(path).close()
    assert connect_ro(path) is not None
    assert not (tmp_path / "we").exists()


def test_snapshot_uid_changes_every_write(conn):
    rid = start_run(conn)
    write_snapshot(conn, make_inventory(), rid, 15)
    first = conn.execute("select value from meta where key='snapshot_uid'").fetchone()[0]
    write_snapshot(conn, make_inventory(), rid, 15)
    assert conn.execute("select value from meta where key='snapshot_uid'").fetchone()[0] != first
