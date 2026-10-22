from datetime import UTC, datetime

from vmdash.collector.transform import (
    RawInventory,
    build_inventory,
    datastore_dir,
    disk_used_bytes,
    folder_path,
)
from vmdash.models import Datastore, Disk, Host, Tag

GB = 2**30
BOOT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
FOLDERS = {
    "group-v1": {"name": "vm", "parent": "datacenter-1"},
    "f2": {"name": "Production", "parent": "group-v1"},
    "f3": {"name": "Web", "parent": "f2"},
}


def raw(vms=None, hosts=None, compute=None, folders=None):
    return RawInventory(
        vcenter_host="vcsa01.lab.local",
        datacenters={"datacenter-2": {"name": "DC-B", "vmFolder": "group-v9"},
                     "datacenter-1": {"name": "DC-Main", "vmFolder": "group-v1"}},
        folders=folders if folders is not None else FOLDERS,
        compute=compute if compute is not None else {"domain-c1": {"name": "Cluster-A", "is_cluster": True},
                                                     "domain-s2": {"name": "esx09.lab.local", "is_cluster": False}},
        hosts=hosts if hosts is not None else {
            "host-1": {"name": "esx01.lab.local", "parent": "domain-c1", "runtime.connectionState": "connected",
                       "runtime.inMaintenanceMode": False},
            "host-9": {"name": "esx09.lab.local", "parent": "domain-s2", "runtime.connectionState": "connected",
                       "runtime.inMaintenanceMode": True},
        },
        datastores={"datastore-1": {"name": "ds-ssd01", "summary.capacity": 2048 * GB, "summary.freeSpace": 900 * GB}},
        vms=vms if vms is not None else {},
    )


FULL_VM = {
    "name": "web01", "parent": "f3", "config.annotation": "Nginx\nfrontend", "config.template": False,
    "config.hardware.numCPU": 4, "config.hardware.memoryMB": 16384,
    "config.hardware.device": [
        {"key": 2000, "label": "Hard disk 1", "capacity_bytes": 60 * GB, "datastore": "datastore-1",
         "file_name": "[ds-ssd01] web01/web01.vmdk"},
        {"key": 2001, "label": "Hard disk 2", "capacity_bytes": 200 * GB, "datastore": "datastore-1",
         "file_name": "[ds-ssd01] web01/web01_1.vmdk"},
    ],
    "config.files.vmPathName": "[ds-ssd01] web01/web01.vmx", "config.guestFullName": "Ubuntu Linux (64-bit)",
    "guest.guestFullName": "Ubuntu 24.04.1 LTS", "runtime.powerState": "poweredOn",
    "runtime.connectionState": "connected", "runtime.host": "host-1", "runtime.bootTime": BOOT,
    "layoutEx": {"files": {1: 30 * GB, 2: 1 * GB, 3: 80 * GB, 4: 5}, "disks": {2000: [1, 2], 2001: [3]}},
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


def test_disk_used_includes_snapshot_chain():
    layout = {"files": {1: 100, 2: 50, 3: 7}, "disks": {2000: [1, 2]}}
    assert disk_used_bytes(2000, layout) == 150
    assert disk_used_bytes(2001, layout) == 0
    assert disk_used_bytes(2000, None) == 0


def test_disk_used_ignores_unknown_file_keys():
    assert disk_used_bytes(2000, {"files": {1: 10}, "disks": {2000: [1, 99]}}) == 10


def test_build_inventory_full_vm():
    inv = build_inventory(raw(vms={"vm-1": FULL_VM}), {"vm-1": [Tag("env", "prod")]})
    assert inv.vcenter_host == "vcsa01.lab.local"
    assert inv.datacenter == "DC-B, DC-Main"
    assert Host("host-1", "esx01.lab.local", "Cluster-A", "connected", False) in inv.hosts
    assert inv.datastores == (Datastore("datastore-1", "ds-ssd01", 2048 * GB, 900 * GB),)
    (vm,) = inv.vms
    assert (vm.moref, vm.name, vm.host_moref, vm.description, vm.folder_path) == (
        "vm-1", "web01", "host-1", "Nginx\nfrontend", "Production/Web")
    assert (vm.power_state, vm.connection_state, vm.is_template, vm.vcpu, vm.memory_mb) == (
        "poweredOn", "connected", False, 4, 16384)
    assert (vm.guest_os, vm.boot_time, vm.datastore_path) == ("Ubuntu 24.04.1 LTS", BOOT, "[ds-ssd01] web01/")
    assert vm.disks == (Disk(2000, "Hard disk 1", 60 * GB, 31 * GB, "ds-ssd01"),
                        Disk(2001, "Hard disk 2", 200 * GB, 80 * GB, "ds-ssd01"))
    assert vm.tags == (Tag("env", "prod"),)


def test_guest_os_fallback():
    vm = dict(FULL_VM, **{"guest.guestFullName": None})
    assert build_inventory(raw(vms={"vm-1": vm}), {}).vms[0].guest_os == "Ubuntu Linux (64-bit)"


def test_standalone_host_has_no_cluster():
    inv = build_inventory(raw(), {})
    assert Host("host-9", "esx09.lab.local", None, "connected", True) in inv.hosts


def test_disk_datastore_falls_back_to_file_name():
    vm = dict(FULL_VM, **{"config.hardware.device": [
        {"key": 2000, "label": "Hard disk 1", "capacity_bytes": GB, "datastore": None, "file_name": "[nfs01] a/a.vmdk"}]})
    assert build_inventory(raw(vms={"vm-1": vm}), {}).vms[0].disks[0].datastore == "nfs01"


def test_inaccessible_vm_is_kept():
    vm = {"name": "x", "runtime.powerState": "poweredOff", "runtime.connectionState": "inaccessible"}
    (out,) = build_inventory(raw(vms={"vm-9": vm}), {}).vms
    assert (out.name, out.vcpu, out.memory_mb, out.disks, out.description, out.datastore_path, out.boot_time) == (
        "x", 0, 0, (), "", "", None)
    assert (out.connection_state, out.host_moref, out.folder_path, out.guest_os, out.is_template) == (
        "inaccessible", None, "", "", False)


def test_vm_without_name_uses_moref():
    assert build_inventory(raw(vms={"vm-7": {}}), {}).vms[0].name == "vm-7"
