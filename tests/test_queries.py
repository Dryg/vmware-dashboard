from datetime import timedelta

from factories import NOW, make_host, make_inventory, make_vm

from vmdash.db import connect_ro, connect_rw, finish_run, read_inventory, start_run, write_snapshot
from vmdash.mock.generate import generate_db
from vmdash.models import Nic, Tag
from vmdash.web.queries import read_status


def test_read_inventory_round_trip(tmp_path):
    inv = make_inventory(
        hosts=[make_host("a"), make_host("b", cluster=None, in_maintenance=True)],
        vms=[make_vm("x", tags=(Tag("env", "prod"), Tag("team", "web")), dns_name="x.lab.local",
                     nics=(Nic(4000, "Network adapter 1", "VLAN10", "00:50:56:00:00:01", True, ("10.0.0.1", "fd00::1")),
                           Nic(4001, "Network adapter 2", "", "", False))),
             make_vm("y", host_moref=None, boot_time=None)],
    )
    conn = connect_rw(tmp_path / "a.db")
    write_snapshot(conn, inv, start_run(conn), 15)
    conn.close()
    assert read_inventory(connect_ro(tmp_path / "a.db")) == inv


def test_read_status_from_mock_db(tmp_path):
    generate_db(tmp_path / "m.db", vms=20, now=NOW)
    st = read_status(connect_ro(tmp_path / "m.db"))
    assert st.snapshot_id == 12
    assert st.last_run.id == 12 and st.last_run.status == "success"
    assert st.last_success_at == NOW
    assert st.interval_minutes == 15
    assert (st.vcenter_host, st.datacenter) == ("vcsa01.lab.local", "DC-Main")


def test_last_success_skips_failed_run(tmp_path):
    conn = connect_rw(tmp_path / "a.db")
    rid = start_run(conn, now=NOW)
    finish_run(conn, rid, "partial", now=NOW)
    write_snapshot(conn, make_inventory(), rid, 15)
    rid2 = start_run(conn, now=NOW + timedelta(minutes=15))
    finish_run(conn, rid2, "failed", error="x", now=NOW + timedelta(minutes=16))
    st = read_status(conn)
    assert st.snapshot_id == rid
    assert st.last_run.status == "failed" and st.last_run.error == "x"
    assert st.last_success_at == NOW


def test_read_status_empty_db(tmp_path):
    connect_rw(tmp_path / "a.db").close()
    st = read_status(connect_ro(tmp_path / "a.db"))
    assert (st.snapshot_id, st.last_run, st.last_success_at, st.interval_minutes) == (None, None, None, 15)


def test_last_success_survives_run_pruning(tmp_path):
    conn = connect_rw(tmp_path / "a.db")
    rid = start_run(conn, now=NOW)
    finish_run(conn, rid, "success", now=NOW)
    write_snapshot(conn, make_inventory(), rid, 15, now=NOW)
    conn.execute("delete from sync_runs where status = 'success'")  # pruned after > 500 failed runs
    finish_run(conn, start_run(conn), "failed", error="x")
    assert read_status(conn).last_success_at == NOW
