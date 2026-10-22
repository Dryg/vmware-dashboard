from datetime import timedelta

from factories import NOW

from vmdash.db import connect_rw
from vmdash.mock.generate import generate_db, generate_inventory, main


def test_deterministic():
    assert generate_inventory(seed=1, now=NOW) == generate_inventory(seed=1, now=NOW)
    assert generate_inventory(seed=1, now=NOW) != generate_inventory(seed=2, now=NOW)


def test_counts():
    inv = generate_inventory(hosts=6, vms=80, now=NOW)
    assert len(inv.hosts) == 6
    assert len(inv.vms) == 80
    assert len({v.moref for v in inv.vms}) == 80
    assert inv.vcenter_host == "vcsa01.lab.local"
    assert inv.datacenter == "DC-Main"


def test_covers_required_cases():
    inv = generate_inventory(now=NOW)
    assert any(h.in_maintenance for h in inv.hosts)
    assert any(h.connection_state == "disconnected" for h in inv.hosts)
    assert any(v.host_moref is None and v.connection_state == "orphaned" for v in inv.vms)
    assert any(v.is_template for v in inv.vms)
    assert {v.power_state for v in inv.vms} >= {"poweredOn", "poweredOff", "suspended"}
    assert {len(v.disks) for v in inv.vms} >= {1, 4}
    assert any(1 - d.free_bytes / d.capacity_bytes > 0.85 for d in inv.datastores)
    assert any("2012 R2" in v.guest_os for v in inv.vms)
    assert any(len(v.description) > 80 for v in inv.vms)
    assert len({t.category for v in inv.vms for t in v.tags}) >= 3
    ages = [NOW - v.boot_time for v in inv.vms
            if v.power_state == "poweredOn" and v.boot_time and not v.is_template]
    day = timedelta(days=1)
    assert any(a < day for a in ages)
    assert any(day <= a < 7 * day for a in ages)
    assert any(7 * day <= a < 30 * day for a in ages)
    assert any(30 * day <= a < 90 * day for a in ages)
    assert any(a >= 90 * day for a in ages)


def test_network_data():
    inv = generate_inventory(now=NOW)
    up = [v for v in inv.vms if v.power_state == "poweredOn" and v.connection_state == "connected"]
    assert all(v.dns_name and all(n.ips for n in v.nics) and v.net_seen_at is None for v in up)
    down = [v for v in inv.vms if v not in up and not v.is_template]
    assert down and all(v.dns_name and v.net_seen_at and v.net_seen_at < NOW for v in down)
    assert all(not v.dns_name and not any(n.ips for n in v.nics) for v in inv.vms if v.is_template)
    assert all(v.nics for v in inv.vms)
    owners = {}
    for v in inv.vms:
        for ip in {ip for n in v.nics for ip in n.ips}:
            owners.setdefault(ip, []).append(v.name)
    assert [names for names in owners.values() if len(names) > 1] == [["old-crm", "db-warehouse01"]]
    assert any(len(v.nics) == 2 for v in up)
    assert any(":" in ip for v in up for n in v.nics for ip in n.ips)
    assert any(not n.connected for v in inv.vms for n in v.nics)


def test_small_counts_still_cover_cases():
    inv = generate_inventory(hosts=4, vms=12, now=NOW)
    assert any(v.host_moref is None for v in inv.vms)
    assert any(v.is_template for v in inv.vms)


def test_disks_reference_existing_datastores():
    inv = generate_inventory(now=NOW)
    names = {d.name for d in inv.datastores}
    for v in inv.vms:
        assert v.datastore_path.startswith("[") and v.datastore_path.endswith("/")
        for d in v.disks:
            assert d.datastore in names
            assert d.capacity_bytes > 0


def test_generate_db_writes_runs(tmp_path):
    generate_db(tmp_path / "m.db", now=NOW)
    conn = connect_rw(tmp_path / "m.db")
    statuses = [r[0] for r in conn.execute("select status from sync_runs order by id")]
    assert len(statuses) == 12
    assert statuses[-1] == "success"
    assert statuses.count("failed") == 1
    assert statuses[-4] == "failed"
    newest = conn.execute("select max(id) from sync_runs").fetchone()[0]
    assert conn.execute("select value from meta where key='snapshot_id'").fetchone()[0] == str(newest)
    assert conn.execute("select count(*) from vms").fetchone()[0] == 80


def test_generate_db_overwrites_existing(tmp_path):
    generate_db(tmp_path / "m.db", vms=20, now=NOW)
    generate_db(tmp_path / "m.db", vms=30, now=NOW)
    conn = connect_rw(tmp_path / "m.db")
    assert conn.execute("select count(*) from sync_runs").fetchone()[0] == 12
    assert conn.execute("select count(*) from vms").fetchone()[0] == 30


def test_main_cli(tmp_path):
    main(["--db", str(tmp_path / "c.db"), "--hosts", "3", "--vms", "15", "--seed", "7"])
    conn = connect_rw(tmp_path / "c.db")
    assert conn.execute("select count(*) from hosts").fetchone()[0] == 3
