"""HTML for VM rows and usage bars, built in Python.

A row is rendered 500 times per page; as Jinja markup it made up most of the server time.
Every value taken from the inventory goes through `escape`.
"""

from markupsafe import Markup, escape

from vmdash.models import Nic
from vmdash.web.views import VmRow, fmt_datetime, fmt_ram, fmt_size, pct


def _bar(used: int, cap: int) -> str:
    p = pct(used, cap)
    return f'<span class="bar{" hi" if p > 85 else ""}" title="{p}% used"><i style="width:{max(p, 3)}%"></i></span>'


def bar(used: int, cap: int) -> Markup:
    return Markup(_bar(used, cap))


def row_numbers(row: VmRow) -> str:
    """data-n attribute read by app.js: vcpu, memory MB, disk bytes, used bytes, running (1/0), host moref."""
    vm = row.vm
    running = int(row.status == "Running")
    return f"{vm.vcpu} {vm.memory_mb} {row.total_cap} {row.total_used} {running} {vm.host_moref or ''}"


def _ip(ip: str, conflicts: dict[str, list[str]]) -> str:
    others = conflicts.get(ip)
    if not others:
        return f'<span class="ip">{escape(ip)}</span>'
    names = escape(", ".join(others))
    return f'<span class="ip dup" title="Also on: {names}">{escape(ip)}</span><span class="dupn">also on {names}</span>'


def _nic(n: Nic, conflicts: dict[str, list[str]]) -> str:
    title = f"{n.label} · {n.mac}" if n.mac else n.label
    off = "" if n.connected else '<span class="sub">disconnected</span>'
    ips = "".join(_ip(ip, conflicts) for ip in n.ips)
    network = escape(n.network or n.label)
    return f'<div class="nic" title="{escape(title)}"><span class="nn">{network}</span>{ips}{off}</div>'


def _network(row: VmRow) -> str:
    vm = row.vm
    dns = f'<div class="dns">{escape(vm.dns_name)}</div>' if vm.dns_name else ""
    nics = "".join(_nic(n, row.ip_conflicts) for n in vm.nics)
    if vm.net_seen_at is None:
        return f'<td class="net">{dns}{nics}</td>'
    seen = fmt_datetime(vm.net_seen_at)
    return (f'<td class="net old">{dns}{nics}<div class="sub" title="The guest reports no network now; '
            f'these are the last values VMware Tools reported">last seen {seen}</div></td>')


def row_html(row: VmRow, show_host: bool) -> Markup:
    vm = row.vm
    desc = escape(vm.description)
    tags = "".join(f'<span class="tag"><i>{escape(t.category)}</i>{escape(t.name)}</span>' for t in vm.tags)
    disks = "".join(
        f'<div class="dk"><span class="dl">Disk {i}</span><span class="dc">{fmt_size(d.capacity_bytes)}</span>'
        f'{_bar(d.used_bytes, d.capacity_bytes)}<span class="du">{fmt_size(d.used_bytes)} used</span></div>'
        for i, d in enumerate(vm.disks, 1)
    )
    host = ""
    if show_host:
        host = f'<td class="hc">{escape(row.host_name)}<div class="sub">{escape(row.cluster)}</div></td>'
    tpl = " tpl" if vm.is_template else ""
    return Markup(
        f'<tr class="vm{tpl}" data-q="{escape(row.search)}" data-n="{escape(row_numbers(row))}">'
        f'<td class="c-vm"><div class="vn"><span class="vname">{escape(vm.name)}</span>'
        f'<span class="ds" title="{desc}">{desc}</span></div>'
        f'<div class="vmeta"><span class="fd">{escape(vm.folder_path)}</span>'
        f'<span>{escape(vm.guest_os)}</span>{tags}</div></td>'
        f'{host}<td><span class="st {row.status_class}">{row.status}</span><div class="sub">{row.uptime}</div></td>'
        f'{_network(row)}'
        f'<td class="num">{vm.vcpu}</td><td class="num">{fmt_ram(vm.memory_mb)}</td>'
        f'<td class="loc">{escape(vm.datastore_path)}</td><td>{disks}</td>'
        f'<td><div class="tc">{fmt_size(row.total_cap)}</div>'
        f'<div class="tu">{_bar(row.total_used, row.total_cap)}'
        f'<span class="du">{fmt_size(row.total_used)}</span></div></td>'
        f"</tr>\n"
    )
