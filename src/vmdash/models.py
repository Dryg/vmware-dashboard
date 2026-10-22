"""Inventory data shared by the collector, the database layer, the web views and the mock generator."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Host:
    moref: str
    name: str
    cluster: str | None
    connection_state: str  # connected | disconnected | notResponding
    in_maintenance: bool


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
    used_bytes: int
    datastore: str


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
    disks: tuple[Disk, ...] = ()
    tags: tuple[Tag, ...] = ()


@dataclass(frozen=True, slots=True)
class Inventory:
    vcenter_host: str
    datacenter: str
    hosts: tuple[Host, ...]
    datastores: tuple[Datastore, ...]
    vms: tuple[Vm, ...]
