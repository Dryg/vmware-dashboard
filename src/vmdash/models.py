"""Inventory data shared by the collector, the database layer, the web views and the mock generator."""

import re
from dataclasses import dataclass
from datetime import datetime


def datastore_name(path: str) -> str:
    """'[ds-ssd01] web01/web01.vmx' -> 'ds-ssd01'"""
    m = re.match(r"\[([^\]]*)\]", path or "")
    return m.group(1) if m else ""


@dataclass(frozen=True, slots=True)
class Host:
    moref: str
    name: str
    connection_state: str  # connected | disconnected | notResponding
    in_maintenance: bool
    # usage at sync time (vCenter quick stats); 0 when the host does not report it (disconnected)
    cpu_used_mhz: int = 0
    cpu_total_mhz: int = 0
    mem_used_bytes: int = 0
    mem_total_bytes: int = 0


@dataclass(frozen=True, slots=True)
class Datastore:
    moref: str
    name: str
    capacity_bytes: int
    free_bytes: int


@dataclass(frozen=True, slots=True)
class Disk:
    key: int
    label: str
    capacity_bytes: int
    datastore: str


@dataclass(frozen=True, slots=True)
class Nic:
    """A virtual network adapter; ips come from VMware Tools and are empty when the guest does not report them."""

    key: int
    label: str
    network: str
    mac: str
    connected: bool
    ips: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Tag:
    category: str
    name: str


@dataclass(frozen=True, slots=True)
class Vm:
    moref: str
    name: str
    host_moref: str | None
    description: str
    folder_path: str
    power_state: str  # poweredOn | poweredOff | suspended
    connection_state: str  # connected | disconnected | orphaned | inaccessible | invalid
    is_template: bool
    vcpu: int
    memory_mb: int
    guest_os: str
    boot_time: datetime | None
    datastore_path: str  # "[ds-ssd01] web01/"
    dns_name: str = ""  # guest host name reported by VMware Tools
    # Set when dns_name and the NIC IPs are last-known values kept from an earlier sync because the guest
    # reports none now (VM off, Tools not running): the time they were last reported. None = reported now.
    net_seen_at: datetime | None = None
    disks: tuple[Disk, ...] = ()
    nics: tuple[Nic, ...] = ()
    tags: tuple[Tag, ...] = ()


@dataclass(frozen=True, slots=True)
class Inventory:
    vcenter_host: str
    datacenter: str
    hosts: tuple[Host, ...]
    datastores: tuple[Datastore, ...]
    vms: tuple[Vm, ...]
