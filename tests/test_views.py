from datetime import timedelta

from factories import GB, NOW, make_host, make_inventory, make_vm

from vmdash.models import Datastore, Disk, Nic, Tag
from vmdash.web.queries import RunInfo, SyncStatus
from vmdash.web.views import (
    GROUPS,
    badge,
    build_groups,
    datastore_name,
    display_status,
    fmt_datetime,
    fmt_ram,
    fmt_size,
    natural_key,
    summary,
    uptime_bucket,
    uptime_text,
)


def running(boot):
    return make_vm(power_state="poweredOn", boot_time=boot)


def inv_with_hosts(*names):
    hosts = [make_host(n) for n in names if n]
    vms = [make_vm(f"vm-{n or 'none'}", host_moref=f"host-{n}" if n else None) for n in names]
    return make_inventory(vms=vms, hosts=hosts)


def keys(groups):
    return [g.key for g in groups]


def test_groups_constant():
    assert list(GROUPS) == ["host", "folder", "cluster", "datastore", "power", "uptime", "os"]
    assert GROUPS["power"] == "Power state" and GROUPS["os"] == "Guest OS"


def test_natural_key():
    assert sorted(["esx10", "esx2", "ESX1"], key=natural_key) == ["ESX1", "esx2", "esx10"]


def test_hosts_natural_sorted_no_host_last():
    groups = build_groups(inv_with_hosts("esx10", "esx2", "ESX1", None), "host", NOW)
    assert keys(groups) == ["ESX1", "esx2", "esx10", "No host"]
    assert groups[0].host is not None and groups[-1].host is None


def test_vms_sorted_inside_group():
    inv = make_inventory(vms=[make_vm("web10"), make_vm("web2"), make_vm("Web1")])
    assert [r.vm.name for r in build_groups(inv, "host", NOW)[0].rows] == ["Web1", "web2", "web10"]


def test_folder_group_uses_first_segment():
    inv = make_inventory(vms=[make_vm("a", folder_path="Production/Web"), make_vm("b", folder_path="Production/DB"),
                              make_vm("c", folder_path="Dev"), make_vm("d", folder_path="")])
    assert keys(build_groups(inv, "folder", NOW)) == ["Dev", "No folder", "Production"]


def test_power_group_order():
    inv = make_inventory(vms=[
        make_vm("t", is_template=True), make_vm("o", connection_state="orphaned"),
        make_vm("s", power_state="suspended"), make_vm("off", power_state="poweredOff"), make_vm("on"),
    ])
    assert keys(build_groups(inv, "power", NOW)) == ["Running", "Suspended", "Shut down", "Orphaned", "Template"]


def test_uptime_buckets():
    assert uptime_bucket(running(NOW - timedelta(hours=5)), NOW) == "Under 1 day"
    assert uptime_bucket(running(NOW - timedelta(days=3)), NOW) == "1–7 days"
    assert uptime_bucket(running(NOW - timedelta(days=7)), NOW) == "7–30 days"
    assert uptime_bucket(running(NOW - timedelta(days=45)), NOW) == "30–90 days"
    assert uptime_bucket(running(NOW - timedelta(days=95)), NOW) == "Over 90 days"
    assert uptime_bucket(make_vm(power_state="poweredOff"), NOW) == "Not running"
    assert uptime_bucket(running(None), NOW) == "Not running"


def test_uptime_group_order():
    inv = make_inventory(vms=[make_vm("a", power_state="poweredOff"), running(NOW - timedelta(days=100)),
                              make_vm("c", boot_time=NOW - timedelta(hours=2))])
    assert keys(build_groups(inv, "uptime", NOW)) == ["Under 1 day", "Over 90 days", "Not running"]


def test_uptime_text():
    assert uptime_text(running(NOW - timedelta(hours=9, minutes=40)), NOW) == "up 9 h"
    assert uptime_text(running(NOW - timedelta(days=23, hours=5)), NOW) == "up 23 d"
    assert uptime_text(running(NOW - timedelta(minutes=10)), NOW) == "up <1 h"
    assert uptime_text(make_vm(power_state="poweredOff"), NOW) == ""


def test_display_status_rules():
    assert display_status(make_vm(is_template=True, connection_state="orphaned")) == "Template"
    assert display_status(make_vm(connection_state="orphaned")) == "Orphaned"
    assert display_status(make_vm(connection_state="disconnected")) == "Unknown"
    assert display_status(make_vm(connection_state="inaccessible")) == "Unknown"
    assert display_status(make_vm(power_state="suspended")) == "Suspended"
    assert display_status(make_vm(power_state="poweredOff")) == "Shut down"
    assert display_status(make_vm()) == "Running"


def test_datastore_name():
    assert datastore_name("[ds-ssd01] web01/") == "ds-ssd01"
    assert datastore_name("") == ""


def test_datastore_group_has_datastore_and_order():
    ds = [Datastore("d1", "ds-b", 100 * GB, 10 * GB), Datastore("d2", "ds-a", 100 * GB, 50 * GB)]
    inv = make_inventory(vms=[make_vm("x", datastore_path="[ds-b] x/"), make_vm("y", datastore_path="[ds-a] y/")],
                         datastores=ds)
    groups = build_groups(inv, "datastore", NOW)
    assert keys(groups) == ["ds-a", "ds-b"]
    assert groups[1].datastore.free_bytes == 10 * GB


def test_cluster_group_no_cluster_last():
    hosts = [make_host("h1", cluster="Cluster-B"), make_host("h2", cluster=None), make_host("h3", cluster="Cluster-A")]
    inv = make_inventory(hosts=hosts, vms=[make_vm(f"v{i}", host_moref=f"host-h{i}") for i in (1, 2, 3)]
                         + [make_vm("orphan", host_moref=None)])
    assert keys(build_groups(inv, "cluster", NOW)) == ["Cluster-A", "Cluster-B", "No cluster"]


def test_os_group():
    inv = make_inventory(vms=[make_vm("a", guest_os="Rocky Linux 9"), make_vm("b", guest_os="")])
    assert keys(build_groups(inv, "os", NOW)) == ["Rocky Linux 9", "Unknown OS"]


def test_unknown_group_falls_back():
    inv = inv_with_hosts("esx1", "esx2")
    assert keys(build_groups(inv, "nope", NOW)) == keys(build_groups(inv, "host", NOW))


def test_group_totals_exclude_templates():
    inv = make_inventory(vms=[make_vm("a", vcpu=4, memory_mb=8192), make_vm("t", is_template=True, vcpu=2)])
    t = build_groups(inv, "host", NOW)[0].totals
    assert (t.vms, t.running, t.vcpu, t.ram_gb, t.cap, t.used) == (1, 1, 4, 8, 60 * GB, 31 * GB)
    assert len(build_groups(inv, "host", NOW)[0].rows) == 2


def test_summary():
    inv = make_inventory(vms=[make_vm("a"), make_vm("b", power_state="poweredOff"), make_vm("t", is_template=True)])
    s = summary(inv)
    assert (s.hosts, s.vms, s.running, s.vcpu) == (1, 2, 1, 8)


def test_row_fields():
    vm = make_vm("a", disks=(Disk(1, "Hard disk 1", 60 * GB, 30 * GB, "ds"), Disk(2, "Hard disk 2", 40 * GB, 10 * GB, "ds")))
    row = build_groups(make_inventory(vms=[vm]), "folder", NOW)[0].rows[0]
    assert (row.host_name, row.cluster, row.status, row.status_class) == ("esx01.lab.local", "Cluster-A", "Running", "ok")
    assert (row.total_cap, row.total_used) == (100 * GB, 40 * GB)
    assert row.uptime == "up 21 d"


def test_search_text_contains_everything():
    vm = make_vm("web01", tags=(Tag("env", "prod"),), guest_os="Ubuntu 24.04 LTS", vcpu=4, memory_mb=16384,
                 dns_name="web01.lab.local",
                 nics=(Nic(4000, "Network adapter 1", "VLAN10-Prod", "00:50:56:AA:00:01", True, ("10.10.0.5",)),))
    s = build_groups(make_inventory(vms=[vm]), "host", NOW)[0].rows[0].search
    for needle in ("web01", "web server", "production/web", "env:prod", "env prod", "ubuntu 24.04", "running",
                   "up 21 d", "esx01.lab.local", "cluster-a", "connected", "[ds-ssd01] web01/", "4 vcpu", "16 gb",
                   "60 gb", "31 gb", "web01.lab.local", "vlan10-prod", "00:50:56:aa:00:01", "10.10.0.5"):
        assert needle in s, needle
    assert s == s.lower()


def test_fmt_size():
    assert fmt_size(60 * GB) == "60 GB"
    assert fmt_size(2048 * GB) == "2 TB"
    assert fmt_size(int(1.74 * 1024 * GB)) == "1.7 TB"
    assert fmt_size(0) == "0 GB"


def test_fmt_ram():
    assert fmt_ram(16384) == "16"
    assert fmt_ram(512) == "0.5"


def test_fmt_datetime():
    assert fmt_datetime(NOW) == "22.10.2026 12:00"


def status(last_run, success_at, snapshot=5, interval=15):
    return SyncStatus(snapshot, last_run, success_at, interval, "vcsa01.lab.local", "DC-Main")


def run(st, start, end=None, error=None):
    return RunInfo(9, st, start, end, error)


def test_badge_ok():
    b = badge(status(run("success", NOW - timedelta(minutes=1), NOW), NOW), NOW + timedelta(minutes=5))
    assert (b.state, b.line1, b.line2, b.banner) == (
        "ok", "Synced 22.10.2026 12:00", "Next sync about 12:15 · page updates itself", None)


def test_badge_running():
    b = badge(status(run("running", NOW + timedelta(minutes=15)), NOW), NOW + timedelta(minutes=15, seconds=10))
    assert (b.state, b.line1, b.line2) == ("running", "Syncing… started 12:15", "Showing data from 12:00")


def test_badge_failed():
    b = badge(status(run("failed", NOW + timedelta(minutes=15), NOW + timedelta(minutes=16), "Boom: no route."), NOW),
              NOW + timedelta(minutes=17))
    assert (b.state, b.line1, b.line2) == ("failed", "Sync failed 12:16", "Showing data from 12:00")
    assert b.banner == "Last sync failed at 22.10.2026 12:16: Boom: no route. Showing data from 12:00."


def test_badge_stale():
    b = badge(status(run("success", NOW, NOW), NOW), NOW + timedelta(minutes=52))
    assert (b.state, b.line1, b.line2) == ("stale", "Data is 52 min old", "Last sync 12:00 · expected every 15 min")
    assert b.banner == ("No successful sync for 52 minutes (expected every 15). "
                        "Check the collector: systemctl status vmdash-collector")


def test_badge_not_stale_at_exactly_three_intervals():
    assert badge(status(run("success", NOW, NOW), NOW), NOW + timedelta(minutes=45)).state == "ok"


def test_badge_none():
    b = badge(SyncStatus(None, None, None, 15, "", ""), NOW)
    assert (b.state, b.line1, b.line2) == ("none", "No sync yet", "Waiting for the collector")


def test_badge_failed_without_snapshot():
    b = badge(SyncStatus(None, run("failed", NOW, NOW, "Boom"), None, 15, "", ""), NOW)
    assert (b.state, b.line2) == ("failed", "No data yet")


def test_natural_key_unicode_digits_do_not_crash():
    assert sorted(["web10", "web1²", "srv-01①", "web2"], key=natural_key) == ["srv-01①", "web1²", "web2", "web10"]
    inv = make_inventory(vms=[make_vm("web1²"), make_vm("web1")])
    assert [r.vm.name for r in build_groups(inv, "host", NOW)[0].rows] == ["web1", "web1²"]


def test_badge_running_too_long_is_stale():
    b = badge(status(run("running", NOW + timedelta(minutes=15)), NOW), NOW + timedelta(days=2))
    assert b.state == "stale"
    assert b.line1 == "Data is 2880 min old"


def test_badge_failed_days_later_shows_date():
    later = NOW + timedelta(days=2)
    b = badge(status(run("failed", later - timedelta(minutes=1), later, "Boom"), NOW), later)
    assert b.line1 == "Sync failed 12:00"
    assert b.line2 == "Showing data from 22.10.2026 12:00"


def test_badge_stale_other_day_shows_date():
    b = badge(status(run("success", NOW, NOW), NOW), NOW + timedelta(days=1))
    assert b.line2 == "Last sync 22.10.2026 12:00 · expected every 15 min"
