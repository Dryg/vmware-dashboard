from datetime import UTC, datetime

from vmdash.collector.transform import (
    RawInventory,
    build_inventory,
    build_nics,
    datastore_dir,
    folder_path,
    keep_last_network,
    useful_ips,
)
from vmdash.models import Datastore, Disk, Host, Nic, Tag

GB = 2**30
BOOT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
FOLDERS = {
    "group-v1": {"name": "vm", "parent": "datacenter-1"},
    "f2": {"name": "Production", "parent": "group-v1"},
    "f3": {"name": "Web", "parent": "f2"},
}


def raw(vms=None, hosts=None, folders=None):
    return RawInventory(
        vcenter_host="vcsa01.lab.local",
        datacenters={"datacenter-2": {"name": "DC-B", "vmFolder": "group-v9"},
                     "datacenter-1": {"name": "DC-Main", "vmFolder": "group-v1"}},
        folders=folders if folders is not None else FOLDERS,
        hosts=hosts if hosts is not None else {
            "host-1": {"name": "esx01.lab.local", "runtime.connectionState": "connected",
                       "runtime.inMaintenanceMode": False},
            "host-9": {"name": "esx09.lab.local", "runtime.connectionState": "connected",
                       "runtime.inMaintenanceMode": True},
        },
        datastores={"datastore-1": {"name": "ds-ssd01", "summary.capacity": 2048 * GB, "summary.freeSpace": 900 * GB}},
        networks={"network-7": {"name": "VM Network"}, "dvportgroup-11": {"name": "VLAN10-Prod"}},
        vms=vms if vms is not None else {},
    )


FULL_VM = {
    "name": "web01", "parent": "f3", "config.annotation": "Nginx\nfrontend", "config.template": False,
    "config.hardware.numCPU": 4, "config.hardware.memoryMB": 16384,
    "config.hardware.device": {"disks": [
        {"key": 2000, "label": "Hard disk 1", "capacity_bytes": 60 * GB, "datastore": "datastore-1",
         "file_name": "[ds-ssd01] web01/web01.vmdk"},
        {"key": 2001, "label": "Hard disk 2", "capacity_bytes": 200 * GB, "datastore": "datastore-1",
         "file_name": "[ds-ssd01] web01/web01_1.vmdk"},
    ], "nics": [
        {"key": 4000, "label": "Network adapter 1", "mac": "00:50:56:aa:00:01", "connected": True,
         "network": "dvportgroup-11", "network_name": ""},
    ]},
    "guest.hostName": "web01.lab.local",
    "guest.net": [{"device_key": 4000, "network": "VLAN10-Prod", "mac": "00:50:56:aa:00:01",
                   "ips": ["10.10.0.5", "fe80::250:56ff:feaa:1"]}],
    "config.files.vmPathName": "[ds-ssd01] web01/web01.vmx", "config.guestFullName": "Ubuntu Linux (64-bit)",
    "guest.guestFullName": "Ubuntu 24.04.1 LTS", "runtime.powerState": "poweredOn",
    "runtime.connectionState": "connected", "runtime.host": "host-1", "runtime.bootTime": BOOT,
}


def test_datastore_dir():
    assert datastore_dir("[ds-ssd01] web01/web01.vmx") == "[ds-ssd01] web01/"
    assert datastore_dir("[ds 2] folder with space/a.vmx") == "[ds 2] folder with space/"
    assert datastore_dir("") == ""


def test_folder_path():
    assert folder_path("f3", FOLDERS, {"group-v1"}) == "Production/Web"
    assert folder_path("group-v1", FOLDERS, {"group-v1"}) == ""
    assert folder_path(None, FOLDERS, {"group-v1"}) == ""


def test_folder_path_stops_on_cycle_or_unknown_parent():
    folders = {"a": {"name": "A", "parent": "b"}, "b": {"name": "B", "parent": "a"}}
    assert folder_path("a", folders, set()) in ("B/A", "A/B/A", "A")  # terminates
    assert folder_path("x", {}, set()) == ""


def test_build_inventory_full_vm():
    inv = build_inventory(raw(vms={"vm-1": FULL_VM}), {"vm-1": [Tag("env", "prod")]})
    assert inv.vcenter_host == "vcsa01.lab.local"
    assert inv.datacenter == "DC-B, DC-Main"
    assert Host("host-1", "esx01.lab.local", "connected", False) in inv.hosts
    assert inv.datastores == (Datastore("datastore-1", "ds-ssd01", 2048 * GB, 900 * GB),)
    (vm,) = inv.vms
    assert (vm.moref, vm.name, vm.host_moref, vm.description, vm.folder_path) == (
        "vm-1", "web01", "host-1", "Nginx\nfrontend", "Production/Web")
    assert (vm.power_state, vm.connection_state, vm.is_template, vm.vcpu, vm.memory_mb) == (
        "poweredOn", "connected", False, 4, 16384)
    assert (vm.guest_os, vm.boot_time, vm.datastore_path) == ("Ubuntu 24.04.1 LTS", BOOT, "[ds-ssd01] web01/")
    assert vm.disks == (Disk(2000, "Hard disk 1", 60 * GB, "ds-ssd01"),
                        Disk(2001, "Hard disk 2", 200 * GB, "ds-ssd01"))
    assert vm.tags == (Tag("env", "prod"),)
    assert vm.dns_name == "web01.lab.local"
    assert vm.nics == (Nic(4000, "Network adapter 1", "VLAN10-Prod", "00:50:56:aa:00:01", True, ("10.10.0.5",)),)


def test_guest_os_fallback():
    vm = dict(FULL_VM, **{"guest.guestFullName": None})
    assert build_inventory(raw(vms={"vm-1": vm}), {}).vms[0].guest_os == "Ubuntu Linux (64-bit)"


def test_host_in_maintenance():
    inv = build_inventory(raw(), {})
    assert Host("host-9", "esx09.lab.local", "connected", True) in inv.hosts


def test_disk_datastore_falls_back_to_file_name():
    vm = dict(FULL_VM, **{"config.hardware.device": {"disks": [
        {"key": 2000, "label": "Hard disk 1", "capacity_bytes": GB, "datastore": None, "file_name": "[nfs01] a/a.vmdk"}]}})
    assert build_inventory(raw(vms={"vm-1": vm}), {}).vms[0].disks[0].datastore == "nfs01"


def test_inaccessible_vm_is_kept():
    vm = {"name": "x", "runtime.powerState": "poweredOff", "runtime.connectionState": "inaccessible"}
    (out,) = build_inventory(raw(vms={"vm-9": vm}), {}).vms
    assert (out.name, out.vcpu, out.memory_mb, out.disks, out.description, out.datastore_path, out.boot_time) == (
        "x", 0, 0, (), "", "", None)
    assert (out.nics, out.dns_name) == ((), "")
    assert (out.connection_state, out.host_moref, out.folder_path, out.guest_os, out.is_template) == (
        "inaccessible", None, "", "", False)


def test_vm_without_name_uses_moref():
    assert build_inventory(raw(vms={"vm-7": {}}), {}).vms[0].name == "vm-7"


def test_useful_ips_ipv4_first_without_link_local():
    assert useful_ips(["fe80::1", "2001:db8::5", "10.0.0.5", "169.254.3.4", "10.0.0.5", "10.0.0.6", "bogus",
                       "fe80::2%eth0"]) == ("10.0.0.5", "10.0.0.6", "2001:db8::5")
    assert useful_ips([]) == ()


def nic(key=4000, **kw):
    return {"key": key, "label": f"Network adapter {key - 3999}", "mac": f"00:50:56:00:00:{key - 4000:02x}",
            "connected": True, "network": None, "network_name": "", **kw}


def test_build_nics_network_name_sources():
    names = {"network-7": "VM Network", "dvportgroup-11": "VLAN10-Prod"}
    nics = build_nics([nic(4000, network="network-7"), nic(4001, network="dvportgroup-11"),
                       nic(4002, network_name="Backing name"), nic(4003)],
                      [{"device_key": 4003, "network": "Guest view", "mac": "", "ips": []}], names)
    assert [n.network for n in nics] == ["VM Network", "VLAN10-Prod", "Backing name", "Guest view"]


def test_build_nics_matches_guest_by_key_then_mac():
    guest = [{"device_key": 4001, "network": "", "mac": "", "ips": ["10.0.0.2"]},
             {"device_key": -1, "network": "", "mac": "00:50:56:00:00:00", "ips": ["10.0.0.1"]},
             {"device_key": -1, "network": "docker0", "mac": "02:42:00:00:00:01", "ips": ["172.17.0.1"]}]
    nics = build_nics([nic(4000, mac="00:50:56:00:00:00"), nic(4001, connected=False)], guest, {})
    assert [(n.ips, n.connected) for n in nics] == [(("10.0.0.1",), True), (("10.0.0.2",), False)]


def test_build_nics_without_guest_info():
    (n,) = build_nics([nic(4000, label="")], [], {})
    assert (n.label, n.network, n.ips) == ("Network adapter 1", "", ())


def test_build_nics_uses_connect_at_power_on_when_not_running():
    devices = [nic(4000, connected=False, start_connected=True), nic(4001, connected=False, start_connected=False)]
    assert [n.connected for n in build_nics(devices, [], {}, running=False)] == [True, False]
    assert [n.connected for n in build_nics(devices, [], {}, running=True)] == [False, False]


SEEN = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def vm_with(ips=(), dns="", mac="00:50:56:00:00:01", key=4000, seen=None):
    from factories import make_vm
    return make_vm("web01", dns_name=dns, net_seen_at=seen,
                   nics=(Nic(key, "Network adapter 1", "VLAN10", mac, True, tuple(ips)),))


def test_keep_last_network_carries_ips_and_dns_when_guest_reports_none():
    from factories import make_inventory
    prev = vm_with(["10.0.0.5"], "web01.lab")
    out = keep_last_network(make_inventory(vms=[vm_with()]), {prev.moref: prev}, SEEN).vms[0]
    assert (out.dns_name, out.nics[0].ips, out.net_seen_at) == ("web01.lab", ("10.0.0.5",), SEEN)


def test_keep_last_network_keeps_original_seen_time_and_matches_by_mac():
    from factories import make_inventory
    older = datetime(2026, 9, 1, tzinfo=UTC)
    prev = vm_with(["10.0.0.5"], "web01.lab", key=4005, seen=older)
    out = keep_last_network(make_inventory(vms=[vm_with()]), {prev.moref: prev}, SEEN).vms[0]
    assert (out.nics[0].ips, out.net_seen_at) == (("10.0.0.5",), older)


def test_keep_last_network_live_data_wins():
    from factories import make_inventory
    prev = vm_with(["10.0.0.5"], "old.lab")
    live = vm_with(["10.0.0.9"], "")
    assert keep_last_network(make_inventory(vms=[live]), {prev.moref: prev}, SEEN).vms[0] == live
    new = vm_with()  # no previous data: unchanged, not marked as last-seen
    assert keep_last_network(make_inventory(vms=[new]), {}, SEEN).vms[0] == new


def test_host_usage():
    host = {"name": "esx01", "runtime.connectionState": "connected",
            "summary.quickStats.overallCpuUsage": 20800, "summary.quickStats.overallMemoryUsage": 262144,
            "summary.hardware.cpuMhz": 2600, "summary.hardware.numCpuCores": 32,
            "summary.hardware.memorySize": 512 * GB}
    (h,) = build_inventory(raw(hosts={"host-1": host}), {}).hosts
    assert (h.cpu_used_mhz, h.cpu_total_mhz, h.mem_used_bytes, h.mem_total_bytes) == (20800, 83200, 256 * GB, 512 * GB)
    (h,) = build_inventory(raw(hosts={"host-1": {"name": "esx01", "runtime.connectionState": "disconnected"}}), {}).hosts
    assert (h.cpu_used_mhz, h.cpu_total_mhz, h.mem_used_bytes, h.mem_total_bytes) == (0, 0, 0, 0)
