from datetime import UTC, datetime

from pyVmomi import vim

from vmdash.collector.vcenter import host_port, plain, to_disks, to_layout

GB = 2**30


def disk(key, label, size, path, ds_moref):
    return vim.vm.device.VirtualDisk(
        key=key, capacityInBytes=size, deviceInfo=vim.Description(label=label, summary=""),
        backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=path, datastore=vim.Datastore(ds_moref)),
    )


def test_to_disks_keeps_only_virtual_disks():
    devices = [disk(2000, "Hard disk 1", 60 * GB, "[ds1] a/a.vmdk", "datastore-1"),
               vim.vm.device.VirtualCdrom(key=3000), vim.vm.device.VirtualE1000(key=4000)]
    assert to_disks(devices) == [{"key": 2000, "label": "Hard disk 1", "capacity_bytes": 60 * GB,
                                  "datastore": "datastore-1", "file_name": "[ds1] a/a.vmdk"}]


def test_to_disks_rdm_without_datastore():
    d = vim.vm.device.VirtualDisk(key=2001, capacityInBytes=GB, deviceInfo=vim.Description(label="Hard disk 2", summary=""),
                                  backing=vim.vm.device.VirtualDisk.RawDiskMappingVer1BackingInfo(fileName="[ds1] a/rdm.vmdk"))
    assert to_disks([d])[0]["datastore"] is None
    assert to_disks(None) == []


def test_to_layout_maps_files_and_chains():
    layout = vim.vm.FileLayoutEx(
        file=[vim.vm.FileLayoutEx.FileInfo(key=1, size=100, name="a.vmdk", type="diskDescriptor"),
              vim.vm.FileLayoutEx.FileInfo(key=2, size=900, name="a-flat.vmdk", type="diskExtent")],
        disk=[vim.vm.FileLayoutEx.DiskLayout(key=2000, chain=[vim.vm.FileLayoutEx.DiskUnit(fileKey=[1, 2])])],
    )
    assert to_layout(layout) == {"files": {1: 100, 2: 900}, "disks": {2000: [1, 2]}}


def test_to_layout_none():
    assert to_layout(None) is None


def test_plain_converts_managed_objects_to_moref():
    assert plain(vim.HostSystem("host-12")) == "host-12"
    when = datetime(2026, 1, 1, tzinfo=UTC)
    assert plain(when) is when
    assert plain("x") == "x"


def test_host_port():
    assert host_port("vcsa01.lab.local") == ("vcsa01.lab.local", 443)
    assert host_port("localhost:8989") == ("localhost", 8989)
