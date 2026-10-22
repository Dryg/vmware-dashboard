import ssl
from datetime import UTC, datetime

from pyVmomi import vim

from vmdash.collector.vcenter import plain, to_devices, to_disks, to_guest_nics, to_layout, to_nics

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


def eth(key, mac, backing, connected=True):
    return vim.vm.device.VirtualVmxnet3(
        key=key, macAddress=mac, backing=backing, deviceInfo=vim.Description(label=f"Network adapter {key - 3999}"),
        connectable=vim.vm.device.VirtualDevice.ConnectInfo(connected=connected, startConnected=connected))


def test_to_nics_standard_and_distributed_port_groups():
    std = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName="VM Network", network=vim.Network("network-7"))
    dvs = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
        port=vim.dvs.PortConnection(portgroupKey="dvportgroup-11", switchUuid="x"))
    devices = [eth(4000, "00:50:56:aa:00:01", std), eth(4001, "00:50:56:aa:00:02", dvs, connected=False),
               disk(2000, "Hard disk 1", GB, "[ds1] a/a.vmdk", "datastore-1")]
    assert to_nics(devices) == [
        {"key": 4000, "label": "Network adapter 1", "mac": "00:50:56:aa:00:01", "connected": True,
         "start_connected": True, "network": "network-7", "network_name": "VM Network"},
        {"key": 4001, "label": "Network adapter 2", "mac": "00:50:56:aa:00:02", "connected": False,
         "start_connected": False, "network": "dvportgroup-11", "network_name": ""},
    ]
    assert [d["key"] for d in to_devices(devices)["disks"]] == [2000]
    assert to_nics(None) == []


def test_to_guest_nics():
    guest = [vim.vm.GuestInfo.NicInfo(deviceConfigId=4000, network="VM Network", macAddress="00:50:56:aa:00:01",
                                      ipAddress=["10.0.0.5", "fe80::1"], connected=True),
             vim.vm.GuestInfo.NicInfo(deviceConfigId=-1, connected=True)]
    assert to_guest_nics(guest) == [
        {"device_key": 4000, "network": "VM Network", "mac": "00:50:56:aa:00:01", "ips": ["10.0.0.5", "fe80::1"]},
        {"device_key": -1, "network": "", "mac": "", "ips": []},
    ]
    assert to_guest_nics(None) == []


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


def test_connect_sets_socket_timeout(monkeypatch):
    from vmdash.collector import vcenter
    from vmdash.config import CollectorSettings
    seen = {}
    monkeypatch.setattr(vcenter, "SmartConnect", lambda **kw: seen.update(kw) or "si")
    s = CollectorSettings(vcenter_host="vc:8443", vcenter_user="u", vcenter_password="p", _env_file=None)
    assert vcenter.connect(s) == "si"
    assert seen["httpConnectionTimeout"] == vcenter.CONNECT_TIMEOUT_S
    assert (seen["host"], seen["port"]) == ("vc", 8443)
    assert seen["sslContext"].verify_mode == ssl.CERT_REQUIRED and seen["disableSslCertValidation"] is False
    insecure = CollectorSettings(vcenter_host="vc", vcenter_user="u", vcenter_password="p",
                                 vcenter_verify_ssl=False, _env_file=None)
    vcenter.connect(insecure)
    assert seen["sslContext"] is None and seen["disableSslCertValidation"] is True


def test_find_datacenter_searches_nested_folders(monkeypatch):
    from types import SimpleNamespace

    from vmdash.collector import vcenter
    root = object()
    si = SimpleNamespace(_stub=None, RetrieveContent=lambda: SimpleNamespace(rootFolder=root))
    calls = []

    def fake_collect(si_, root_, obj_type, paths, extra=None):
        calls.append((root_, obj_type))
        return {"datacenter-3": {"name": "DC1", "vmFolder": "group-v4"},
                "datacenter-9": {"name": "DC2", "vmFolder": "group-v9"}}
    monkeypatch.setattr(vcenter, "_collect", fake_collect)
    dc, found = vcenter.find_datacenter(si, "DC2")
    assert dc._moId == "datacenter-9"
    assert found == {"datacenter-9": {"name": "DC2", "vmFolder": "group-v9"}}
    assert calls == [(root, vim.Datacenter)]
    import pytest
    with pytest.raises(ValueError, match="Datacenter not found: Nope"):
        vcenter.find_datacenter(si, "Nope")
