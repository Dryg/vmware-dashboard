"""Deterministic mock inventory and database for development and tests."""

import argparse
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vmdash.db import connect_rw, finish_run, start_run, write_snapshot
from vmdash.models import Datastore, Disk, Host, Inventory, Nic, Tag, Vm

GB = 2**30
TB = 2**40

W22, W19, W12 = "Microsoft Windows Server 2022 (64-bit)", "Microsoft Windows Server 2019 (64-bit)", \
    "Microsoft Windows Server 2012 R2 (64-bit)"
U24, U22, D12, R9 = "Ubuntu Linux (64-bit) 24.04 LTS", "Ubuntu Linux (64-bit) 22.04 LTS", \
    "Debian GNU/Linux 12 (64-bit)", "Rocky Linux 9 (64-bit)"

# prefix, description, folder, OS, vCPU, RAM GB, data disk sizes (GB), extra tags
ROLES = [
    ("web", "Nginx frontend, public site", "Production/Web", U24, 4, 16, [200], [("owner", "ops")]),
    ("app", "Order service (Java 21)", "Production/App", R9, 6, 24, [150], [("team", "shop")]),
    ("db", "PostgreSQL 16 node", "Production/DB", R9, 8, 64, [500, 100], [("backup", "daily")]),
    ("k8s-node", "Kubernetes worker", "Production/K8s", U24, 8, 32, [100], [("team", "platform")]),
    ("mq", "RabbitMQ broker", "Production/App", D12, 2, 8, [100], []),
    ("files", "SMB file share for departments", "Production/Files", W19, 4, 16, [2048], [("backup", "daily")]),
    ("dc", "Active Directory domain controller", "Infra/Identity", W22, 2, 8, [], [("backup", "daily")]),
    ("mon", "Prometheus + Grafana", "Infra/Monitoring", U24, 4, 16, [500], [("team", "platform")]),
    ("vpn", "WireGuard gateway", "Infra", D12, 1, 2, [], []),
    ("jump", "Admin jump host", "Infra", D12, 2, 4, [], [("owner", "ops")]),
    ("build", "GitLab runner, Docker executor", "Dev/CI", U22, 8, 32, [300], [("team", "platform")]),
    ("test", "QA sandbox", "Dev/Test", U22, 2, 8, [], []),
    ("etl", "Nightly data import jobs", "Production/Data", R9, 4, 16, [250], [("team", "data")]),
]
ENV_BY_FOLDER = {"Production": "prod", "Infra": "prod", "Dev": "dev", "Archive": "legacy"}

# top-level folder -> port group, IPv4 prefix; DB servers also sit on the backup network
NETWORKS = {"Production": ("VLAN10-Prod", "10.10"), "Infra": ("VLAN20-Infra", "10.20"),
            "Dev": ("VLAN30-Dev", "10.30")}
OTHER_NETWORK, BACKUP_NETWORK = ("VM Network", "192.168"), ("VLAN90-Backup", "10.90")

DATASTORES = [("ds-ssd01", 2 * TB, 0.45), ("ds-ssd02", 2 * TB, 0.22), ("ds-ssd03", 2 * TB, 0.55),
              ("ds-hdd01", 4 * TB, 0.70), ("ds-hdd02", 4 * TB, 0.09)]

FAILED_RUN_ERROR = "Cannot connect to vcsa01.lab.local:443 (timed out after 30 s)"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC).replace(second=0, microsecond=0)


def _hosts(n: int) -> list[Host]:
    hosts = []
    for i in range(n):
        name = f"esx{i + 1:02d}.lab.local"
        state = "disconnected" if n >= 3 and i == n - 1 else "connected"
        hosts.append(Host(f"host-{100 + i}", name, "Cluster-A" if i < (n + 1) // 2 else "Cluster-B",
                          state, n >= 2 and i == 1))
    return hosts


# old-crm's last-known IP has since been given to the VM with this index (db-warehouse01): an IP conflict
CONFLICT_VM, CONFLICT_INDEX = "old-crm", 5


def _nics(i: int, name: str, folder: str, has_ips: bool, connected: bool = True) -> tuple[Nic, ...]:
    """Deterministic adapters; has_ips is False only for templates, which never reported any."""
    nets, n = [NETWORKS.get(folder.split("/")[0], OTHER_NETWORK)], i
    if name == CONFLICT_VM:
        nets, n = [NETWORKS["Production"]], CONFLICT_INDEX
    if name.startswith("db"):
        nets.append(BACKUP_NETWORK)
    nics = []
    for k, (network, prefix) in enumerate(nets):
        ips: tuple[str, ...] = ()
        if has_ips:
            ips = (f"{prefix}.{n // 250}.{n % 250 + 4}",)
            if name.startswith("web"):
                ips += (f"fd00:{prefix.split('.')[1]}::{n + 4:x}",)
        nics.append(Nic(4000 + k, f"Network adapter {k + 1}", network, f"00:50:56:{i >> 8:02x}:{i & 255:02x}:{k:02x}",
                        connected, ips))
    return tuple(nics)


def generate_inventory(hosts: int = 6, vms: int = 80, seed: int = 42, now: datetime | None = None) -> Inventory:
    rng = random.Random(seed)
    now = _now(now)
    host_list = _hosts(hosts)
    datastores = [Datastore(f"datastore-{i + 1}", name, cap, int(cap * free))
                  for i, (name, cap, free) in enumerate(DATASTORES)]
    ds_names = [d.name for d in datastores]
    counters: dict[str, int] = {}
    out: list[Vm] = []

    def disks(sizes: list[int], ds: str) -> tuple[Disk, ...]:
        result = []
        for i, size in enumerate(sizes):
            used = int(size * GB * rng.uniform(0.1, 0.95))
            result.append(Disk(2000 + i, f"Hard disk {i + 1}", size * GB, used, ds))
        return tuple(result)

    def make(i: int, name: str, desc: str, folder: str, os_: str, cpu: int, ram: int, sizes: list[int],
             tags: list[tuple[str, str]], power: str | None = None, boot_age: timedelta | None = None,
             host: Host | None | bool = True, template: bool = False, conn: str = "connected") -> Vm:
        if host is True:
            host = host_list[rng.randrange(len(host_list))]
        if power is None:
            r = rng.random()
            power = "poweredOn" if r < 0.8 else "poweredOff" if r < 0.95 else "suspended"
        if host and host.in_maintenance and power == "poweredOn":
            power = "poweredOff"
        if host and host.connection_state == "disconnected":
            conn = "disconnected"
        if template:
            power = "poweredOff"
        boot = None
        if power == "poweredOn":
            boot = now - (boot_age if boot_age is not None else timedelta(hours=rng.uniform(2, 200 * 24)))
        ds = rng.choice(ds_names[:3]) if folder.startswith("Production") else rng.choice(ds_names)
        guest_up = power == "poweredOn" and conn == "connected"
        all_tags = list(tags)
        env = ENV_BY_FOLDER.get(folder.split("/")[0])
        if folder == "Dev/Test":
            env = "test"
        if env and not template:
            all_tags.insert(0, ("env", env))
        return Vm(
            moref=f"vm-{1000 + i}", name=name, host_moref=host.moref if host else None, description=desc,
            folder_path=folder, power_state=power, connection_state=conn, is_template=template,
            vcpu=cpu, memory_mb=ram * 1024, guest_os=os_, boot_time=boot, datastore_path=f"[{ds}] {name}/",
            dns_name="" if template else f"{name}.lab.local",
            # VMs whose guest reports nothing now show what was last reported, up to a month ago
            net_seen_at=None if guest_up or template else now - timedelta(days=i % 30 + 1, hours=i % 24),
            disks=disks([rng.choice([40, 60, 80, 100])] + sizes, ds),
            nics=_nics(i, name, folder, has_ips=not template, connected=name != CONFLICT_VM),
            tags=tuple(Tag(c, v) for c, v in sorted(all_tags)),
        )

    forced = [
        lambda i: make(i, "restore-test-0915", "Left over after host esx99 was removed",
                       "Discovered virtual machine", U22, 2, 4, [], [], power="poweredOff",
                       host=None, conn="orphaned"),
        lambda i: make(i, "tpl-ubuntu-24.04", "Ubuntu 24.04 LTS base image", "Templates", U24, 2, 4, [], [],
                       template=True),
        lambda i: make(i, "tpl-win2022", "Windows Server 2022 Standard, sysprepped", "Templates", W22, 4, 8,
                       [], [], template=True),
        lambda i: make(i, "old-crm", "Legacy CRM. Decommission after the Q4 migration; do not power on "
                       "without asking the finance team first.", "Archive", W12, 2, 8, [120], [],
                       power="poweredOff"),
        lambda i: make(i, "test-suspended01", "QA sandbox, paused for investigation", "Dev/Test", U22, 2, 8,
                       [], [], power="suspended", host=host_list[0]),
        lambda i: make(i, "db-warehouse01", "Analytics warehouse (4 disks)", "Production/DB", R9, 16, 128,
                       [500, 500, 200], [("team", "data"), ("backup", "daily")], power="poweredOn",
                       host=host_list[0]),
    ]
    for hours in (5, 3 * 24, 15 * 24, 45 * 24, 120 * 24):
        forced.append(lambda i, h=hours: _role_vm(i, h))

    def _role_vm(i: int, boot_hours: float | None = None) -> Vm:
        prefix, desc, folder, os_, cpu, ram, sizes, tags = rng.choice(ROLES)
        counters[prefix] = counters.get(prefix, 0) + 1
        name = f"{prefix}{counters[prefix]:02d}"
        if boot_hours is None:
            return make(i, name, desc, folder, os_, cpu, ram, sizes, tags)
        return make(i, name, desc, folder, os_, cpu, ram, sizes, tags, power="poweredOn",
                    boot_age=timedelta(hours=boot_hours), host=host_list[0])

    for i in range(vms):
        out.append(forced[i](i) if i < len(forced) else _role_vm(i))

    return Inventory("vcsa01.lab.local", "DC-Main", tuple(host_list), tuple(datastores), tuple(out))


def generate_db(path: Path, hosts: int = 6, vms: int = 80, seed: int = 42, now: datetime | None = None) -> None:
    now = _now(now)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    inv = generate_inventory(hosts, vms, seed, now)
    conn = connect_rw(path)
    try:
        run_id = 0
        for i in range(12):
            finished = now - timedelta(minutes=15 * (11 - i))
            run_id = start_run(conn, now=finished - timedelta(seconds=40))
            if i == 8:
                finish_run(conn, run_id, "failed", error=FAILED_RUN_ERROR, now=finished)
            else:
                finish_run(conn, run_id, "success", host_count=len(inv.hosts), vm_count=len(inv.vms),
                           now=finished)
        write_snapshot(conn, inv, run_id, 15, now=now)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="vmdash-mockdb", description="Write a mock vmdash SQLite database.")
    p.add_argument("--db", type=Path, default=Path("mock.db"))
    p.add_argument("--hosts", type=int, default=6)
    p.add_argument("--vms", type=int, default=80)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args(argv)
    generate_db(a.db, a.hosts, a.vms, a.seed)
    print(f"wrote {a.db}: {a.hosts} hosts, {a.vms} VMs (seed {a.seed})")
