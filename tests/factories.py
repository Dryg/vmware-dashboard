"""Test helpers that build model objects with sensible defaults."""

from datetime import UTC, datetime

from vmdash.models import Datastore, Disk, Host, Inventory, Tag, Vm

GB = 2**30
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def make_host(name="esx01.lab.local", **kw) -> Host:
    d = dict(moref=f"host-{name}", name=name, cluster="Cluster-A", connection_state="connected",
             in_maintenance=False)
    d.update(kw)
    return Host(**d)


def make_vm(name="web01", **kw) -> Vm:
    d = dict(
        moref=f"vm-{name}", name=name, host_moref="host-esx01.lab.local", description="Web server",
        folder_path="Production/Web", power_state="poweredOn", connection_state="connected",
        is_template=False, vcpu=4, memory_mb=16384, guest_os="Ubuntu 24.04 LTS",
        boot_time=datetime(2026, 10, 1, 8, 0, tzinfo=UTC), datastore_path=f"[ds-ssd01] {name}/",
        disks=(Disk(2000, "Hard disk 1", 60 * GB, 31 * GB, "ds-ssd01"),),
        tags=(Tag("env", "prod"),),
    )
    d.update(kw)
    return Vm(**d)


def make_inventory(vms=None, hosts=None, datastores=None, **kw) -> Inventory:
    d = dict(
        vcenter_host="vcsa01.lab.local", datacenter="DC-Main",
        hosts=tuple(hosts) if hosts is not None else (make_host(),),
        datastores=tuple(datastores) if datastores is not None
        else (Datastore("datastore-1", "ds-ssd01", 2048 * GB, 900 * GB),),
        vms=tuple(vms) if vms is not None else (make_vm(),),
    )
    d.update(kw)
    return Inventory(**d)
