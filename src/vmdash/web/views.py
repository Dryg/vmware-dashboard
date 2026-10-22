"""Pure view logic: grouping, ordering, display status, totals and the sync badge."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from vmdash.config import https_url
from vmdash.models import Datastore, Host, Inventory, Vm, datastore_name
from vmdash.web.queries import SyncStatus

GROUPS: dict[str, str] = {
    "host": "Host", "folder": "Folder", "datastore": "Datastore",
    "power": "Power state", "uptime": "Uptime", "os": "Guest OS",
}
POWER_ORDER = ["Running", "Suspended", "Shut down", "Unknown", "Orphaned", "Template"]
UPTIME_BUCKETS = ["Under 1 day", "1–7 days", "7–30 days", "30–90 days", "Over 90 days", "Not running"]
STATUS_CLASS = {"Running": "ok", "Suspended": "warn", "Shut down": "off", "Unknown": "bad", "Orphaned": "bad",
                "Template": "tpl"}
NO_HOST, NO_FOLDER, UNKNOWN_OS, UNKNOWN_DS = "No host", "No folder", "Unknown OS", \
    "Unknown datastore"
GB = 2**30


# ---------- formatting ----------

def fmt_size(n_bytes: int) -> str:
    gb = n_bytes / GB
    if gb >= 1024:
        return f"{gb / 1024:.1f}".removesuffix(".0") + " TB"
    return f"{round(gb)} GB"


def fmt_ram(memory_mb: int) -> str:
    return f"{memory_mb / 1024:.1f}".removesuffix(".0")


def fmt_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%H:%M")


def fmt_datetime(dt: datetime) -> str:
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def fmt_when(dt: datetime, now: datetime) -> str:
    """HH:MM for today, full date otherwise."""
    local = dt.astimezone()
    return fmt_time(dt) if local.date() == now.astimezone().date() else fmt_datetime(dt)


def vcenter_url(host: str) -> str:
    """vSphere Client URL for VCENTER_HOST ("vc", "vc:8443", "[2001:db8::5]:443"); "" before the first sync."""
    return https_url(host).removesuffix(":443") + "/ui/" if host else ""


def ghz(mhz: int) -> str:
    return f"{mhz / 1000:.1f}".removesuffix(".0") + " GHz"


def pct(part: int, whole: int) -> int:
    return round(part * 100 / whole) if whole else 0


# ---------- per-VM facts ----------

def natural_key(s: str) -> tuple:
    # re.split with a capture group puts the digit runs at odd positions; str.isdigit() would also accept
    # characters like "²" that int() rejects.
    return tuple(int(p) if i % 2 else p for i, p in enumerate(re.split(r"([0-9]+)", s.casefold())))


def display_status(vm: Vm) -> str:
    if vm.is_template:
        return "Template"
    if vm.connection_state == "orphaned":
        return "Orphaned"
    if vm.connection_state in ("disconnected", "inaccessible", "invalid"):
        return "Unknown"
    return {"poweredOn": "Running", "suspended": "Suspended"}.get(vm.power_state, "Shut down")


def status_class(status: str) -> str:
    return STATUS_CLASS[status]


def _uptime(vm: Vm, now: datetime) -> timedelta | None:
    if display_status(vm) != "Running" or vm.boot_time is None:
        return None
    return max(now - vm.boot_time, timedelta(0))


def uptime_bucket(vm: Vm, now: datetime) -> str:
    up = _uptime(vm, now)
    if up is None:
        return "Not running"
    days = up / timedelta(days=1)
    for limit, label in ((1, "Under 1 day"), (7, "1–7 days"), (30, "7–30 days"), (90, "30–90 days")):
        if days < limit:
            return label
    return "Over 90 days"


def uptime_text(vm: Vm, now: datetime) -> str:
    up = _uptime(vm, now)
    if up is None:
        return ""
    if up < timedelta(hours=1):
        return "up <1 h"
    if up < timedelta(days=1):
        return f"up {up // timedelta(hours=1)} h"
    return f"up {up.days} d"


def host_state(host: Host) -> tuple[str, str]:
    """(label, css class) for a host's connection state."""
    if host.connection_state == "connected":
        return ("Maintenance mode", "warn") if host.in_maintenance else ("Connected", "ok")
    if host.connection_state == "notResponding":
        return "Not responding", "bad"
    return "Disconnected", "bad"


# ---------- rows, groups, totals ----------

@dataclass(frozen=True, slots=True)
class VmRow:
    vm: Vm
    host_name: str
    status: str
    status_class: str
    uptime: str
    total_cap: int
    search: str
    ip_conflicts: dict[str, list[str]]  # IP -> names of the other VMs that have it


def ip_owners(inv: Inventory) -> dict[str, list[str]]:
    """IP -> names of the VMs that have it, current or last-known (an off VM's IP may have been reused)."""
    owners: dict[str, list[str]] = {}
    for vm in inv.vms:
        for ip in {ip for n in vm.nics for ip in n.ips}:
            owners.setdefault(ip, []).append(vm.name)
    return owners


@dataclass(frozen=True, slots=True)
class Totals:
    vms: int
    running: int
    vcpu: int
    ram_gb: int
    cap: int
    hosts: int


@dataclass(frozen=True, slots=True)
class Group:
    key: str
    label: str
    rows: list[VmRow]
    totals: Totals
    host: Host | None
    datastore: Datastore | None


def _row(vm: Vm, hosts: dict[str, Host], owners: dict[str, list[str]], now: datetime) -> VmRow:
    host = hosts.get(vm.host_moref or "")
    conflicts = {ip: [o for o in owners[ip] if o != vm.name] or [vm.name]
                 for n in vm.nics for ip in n.ips if len(owners.get(ip, ())) > 1}
    status = display_status(vm)
    uptime = uptime_text(vm, now)
    cap = sum(d.capacity_bytes for d in vm.disks)
    parts = [
        vm.name, vm.description, vm.folder_path, vm.guest_os, status, uptime,
        host.name if host else NO_HOST, host_state(host)[0] if host else "",
        vm.datastore_path, f"{vm.vcpu} vcpu", f"{fmt_ram(vm.memory_mb)} gb", vm.dns_name,
        *(f"{n.network} {n.mac} {' '.join(n.ips)}" for n in vm.nics),
        "last seen" if vm.net_seen_at else "", "ip conflict" if conflicts else "",
        *(f"{t.category}:{t.name} {t.category} {t.name}" for t in vm.tags),
        *(fmt_size(d.capacity_bytes) for d in vm.disks),
    ]
    return VmRow(vm, host.name if host else NO_HOST, status,
                 status_class(status), uptime, cap, " ".join(p for p in parts if p).lower(), conflicts)


def _totals(rows: list[VmRow]) -> Totals:
    live = [r for r in rows if not r.vm.is_template]
    return Totals(
        vms=len(live),
        running=sum(r.status == "Running" for r in live),
        vcpu=sum(r.vm.vcpu for r in live),
        ram_gb=round(sum(r.vm.memory_mb for r in live) / 1024),
        cap=sum(r.total_cap for r in live),
        hosts=len({r.vm.host_moref for r in live if r.vm.host_moref}),
    )


def _last(name: str) -> Callable[[str], tuple]:
    return lambda k: (k == name, natural_key(k))


FOLDERS_FIRST = ["production", "test"]  # top-level folders shown first, in this order (any letter case)


def _folder_order(k: str) -> tuple:
    f = k.casefold()
    return (k == NO_FOLDER, FOLDERS_FIRST.index(f) if f in FOLDERS_FIRST else len(FOLDERS_FIRST), natural_key(k))


def build_groups(inv: Inventory, group: str, now: datetime) -> list[Group]:
    if group not in GROUPS:
        group = "host"
    hosts = {h.moref: h for h in inv.hosts}
    hosts_by_name = {h.name: h for h in inv.hosts}
    datastores = {d.name: d for d in inv.datastores}

    key_fn: Callable[[VmRow], str] = {
        "host": lambda r: r.host_name,
        "folder": lambda r: r.vm.folder_path.split("/")[0] or NO_FOLDER,
        "datastore": lambda r: datastore_name(r.vm.datastore_path) or UNKNOWN_DS,
        "power": lambda r: r.status,
        "uptime": lambda r: uptime_bucket(r.vm, now),
        "os": lambda r: r.vm.guest_os or UNKNOWN_OS,
    }[group]
    order: Callable[[str], tuple] = {
        "host": _last(NO_HOST),
        "folder": _folder_order,
        "datastore": _last(UNKNOWN_DS),
        "os": _last(UNKNOWN_OS),
        "power": lambda k: (POWER_ORDER.index(k),),
        "uptime": lambda k: (UPTIME_BUCKETS.index(k),),
    }[group]

    owners = ip_owners(inv)
    buckets: dict[str, list[VmRow]] = {}
    for vm in inv.vms:
        row = _row(vm, hosts, owners, now)
        buckets.setdefault(key_fn(row), []).append(row)

    groups = []
    for key in sorted(buckets, key=order):
        rows = sorted(buckets[key], key=lambda r: natural_key(r.vm.name))
        groups.append(Group(
            key=key, label=key, rows=rows, totals=_totals(rows),
            host=hosts_by_name.get(key) if group == "host" else None,
            datastore=datastores.get(key) if group == "datastore" else None,
        ))
    return groups


def totals_of(groups: list[Group]) -> Totals:
    return _totals([r for g in groups for r in g.rows])


# ---------- sync badge ----------

@dataclass(frozen=True, slots=True)
class Badge:
    state: str  # ok | partial | running | failed | stale | none
    line1: str
    line2: str
    banner: str | None


def badge(st: SyncStatus, now: datetime) -> Badge:
    run, ok_at = st.last_run, st.last_success_at
    has_data = bool(st.snapshot_id and ok_at)
    data_from = f"Showing data from {fmt_when(ok_at, now)}" if has_data else "No data yet"
    too_old = has_data and now - ok_at > timedelta(minutes=3 * st.interval_minutes)
    if run and run.status == "failed":
        at = run.finished_at or run.started_at
        error = (run.error or "unknown error").rstrip(". ")
        tail = f" {data_from}." if has_data else ""
        return Badge("failed", f"Sync failed {fmt_when(at, now)}", data_from,
                     f"Last sync failed at {fmt_datetime(at)}: {error}.{tail}")
    if run and run.status == "partial" and not too_old:
        at = run.finished_at or run.started_at
        error = (run.error or "unknown error").removeprefix("tags: ").rstrip(". ")
        return Badge("partial", f"Synced {fmt_when(at, now)}, tags failed", "Tags are from an earlier sync",
                     f"Tags could not be read at {fmt_datetime(at)}: {error}. VM data is current; "
                     "tags shown are from the last sync that read them.")
    if run and run.status == "running" and not too_old:
        return Badge("running", f"Syncing… started {fmt_when(run.started_at, now)}", data_from, None)
    if not has_data:
        return Badge("none", "No sync yet", "Waiting for the collector", None)
    if too_old:  # also covers a sync that has been "running" far too long (hung collector)
        mins = int((now - ok_at) / timedelta(minutes=1))
        return Badge(
            "stale", f"Data is {mins} min old",
            f"Last sync {fmt_when(ok_at, now)} · expected every {st.interval_minutes} min",
            f"No successful sync for {mins} minutes (expected every {st.interval_minutes}). "
            "Check the collector: systemctl status vmdash-collector",
        )
    nxt = ok_at + timedelta(minutes=st.interval_minutes)
    return Badge("ok", f"Synced {fmt_datetime(ok_at)}", f"Next sync {fmt_time(nxt)}", None)
