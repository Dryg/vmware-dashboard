"""Turn raw vCenter property data (plain dicts, see vcenter.py) into an Inventory. Pure functions."""

import ipaddress
import re
from dataclasses import dataclass, field

from vmdash.models import Datastore, Disk, Host, Inventory, Nic, Tag, Vm


@dataclass
class RawInventory:
    """Per object type: {moref: {property path: plain value}}. References are moref strings."""

    vcenter_host: str
    datacenters: dict[str, dict] = field(default_factory=dict)
    folders: dict[str, dict] = field(default_factory=dict)
    compute: dict[str, dict] = field(default_factory=dict)  # {"name", "is_cluster"}
    hosts: dict[str, dict] = field(default_factory=dict)
    datastores: dict[str, dict] = field(default_factory=dict)
    networks: dict[str, dict] = field(default_factory=dict)  # {"name"}
    vms: dict[str, dict] = field(default_factory=dict)


def datastore_dir(vm_path_name: str) -> str:
    """'[ds1] web01/web01.vmx' -> '[ds1] web01/'"""
    if not vm_path_name:
        return ""
    head, sep, _ = vm_path_name.rpartition("/")
    return head + sep if sep else vm_path_name


def folder_path(folder_moref: str | None, folders: dict[str, dict], roots: set[str]) -> str:
    names: list[str] = []
    seen: set[str] = set()
    current = folder_moref
    while current and current not in roots and current in folders and current not in seen:
        seen.add(current)
        names.append(folders[current].get("name", ""))
        current = folders[current].get("parent")
    return "/".join(reversed(names))


def disk_used_bytes(disk_key: int, layout: dict | None) -> int:
    """Sum of every file in the disk's chain (base disk plus snapshot deltas)."""
    if not layout:
        return 0
    files = layout.get("files", {})
    return sum(files.get(k, 0) for k in layout.get("disks", {}).get(disk_key, []))


def useful_ips(ips: list[str]) -> tuple[str, ...]:
    """IPv4 before IPv6, otherwise in reported order; no link-local (fe80::/10, 169.254/16) or duplicates."""
    out: dict[str, int] = {}
    for text in ips:
        try:
            ip = ipaddress.ip_address(text.split("%")[0])
        except ValueError:
            continue
        if not (ip.is_link_local or ip.is_unspecified or ip.is_loopback):
            out.setdefault(str(ip), ip.version)
    return tuple(sorted(out, key=out.get))


def build_nics(devices: list[dict], guest: list[dict], network_names: dict[str, str]) -> tuple[Nic, ...]:
    """Virtual adapters with the IPs VMware Tools reports for them (matched by device key, else by MAC)."""
    by_key = {g["device_key"]: g for g in guest}
    by_mac = {g["mac"].lower(): g for g in guest if g.get("mac")}
    nics = []
    for i, d in enumerate(devices, 1):
        g = by_key.get(d["key"]) or by_mac.get((d.get("mac") or "").lower()) or {}
        network = (network_names.get(d.get("network") or "") or d.get("network_name")
                   or g.get("network") or "")
        nics.append(Nic(key=d["key"], label=d.get("label") or f"Network adapter {i}", network=network,
                        mac=d.get("mac") or "", connected=bool(d.get("connected")),
                        ips=useful_ips(g.get("ips") or [])))
    return tuple(nics)


def _bracket_name(path: str) -> str:
    m = re.match(r"\[([^\]]*)\]", path or "")
    return m.group(1) if m else ""


def build_inventory(raw: RawInventory, tags: dict[str, list[Tag]]) -> Inventory:
    roots = {dc.get("vmFolder") for dc in raw.datacenters.values() if dc.get("vmFolder")}
    ds_names = {moref: d.get("name", moref) for moref, d in raw.datastores.items()}
    net_names = {moref: n.get("name") or "" for moref, n in raw.networks.items()}

    hosts = []
    for moref, h in raw.hosts.items():
        compute = raw.compute.get(h.get("parent") or "", {})
        hosts.append(Host(
            moref=moref, name=h.get("name") or moref,
            cluster=compute.get("name") if compute.get("is_cluster") else None,
            connection_state=h.get("runtime.connectionState") or "connected",
            in_maintenance=bool(h.get("runtime.inMaintenanceMode")),
        ))

    datastores = [
        Datastore(moref, d.get("name") or moref,
                  int(d.get("summary.capacity") or 0), int(d.get("summary.freeSpace") or 0))
        for moref, d in raw.datastores.items()
    ]

    vms = []
    for moref, v in raw.vms.items():
        layout = v.get("layoutEx")
        devices = v.get("config.hardware.device") or {}
        disks = tuple(
            Disk(
                key=d["key"], label=d.get("label") or f"Disk {i}", capacity_bytes=int(d.get("capacity_bytes") or 0),
                used_bytes=disk_used_bytes(d["key"], layout),
                datastore=ds_names.get(d.get("datastore") or "") or _bracket_name(d.get("file_name", "")),
            )
            for i, d in enumerate(devices.get("disks") or [], 1)
        )
        vms.append(Vm(
            moref=moref, name=v.get("name") or moref, host_moref=v.get("runtime.host"),
            description=v.get("config.annotation") or "",
            folder_path=folder_path(v.get("parent"), raw.folders, roots),
            power_state=v.get("runtime.powerState") or "poweredOff",
            connection_state=v.get("runtime.connectionState") or "connected",
            is_template=bool(v.get("config.template")),
            vcpu=int(v.get("config.hardware.numCPU") or 0), memory_mb=int(v.get("config.hardware.memoryMB") or 0),
            guest_os=v.get("guest.guestFullName") or v.get("config.guestFullName") or "",
            boot_time=v.get("runtime.bootTime"),
            datastore_path=datastore_dir(v.get("config.files.vmPathName") or ""),
            dns_name=v.get("guest.hostName") or "",
            disks=disks, nics=build_nics(devices.get("nics") or [], v.get("guest.net") or [], net_names),
            tags=tuple(tags.get(moref, ())),
        ))

    datacenter = ", ".join(sorted(dc.get("name", "") for dc in raw.datacenters.values()))
    return Inventory(raw.vcenter_host, datacenter, tuple(hosts), tuple(datastores), tuple(vms))
