"""Read the inventory from vCenter with pyVmomi: one PropertyCollector call per object type."""

import logging

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl
from pyVmomi.VmomiSupport import ManagedObject

from vmdash.collector.transform import RawInventory
from vmdash.config import CollectorSettings

log = logging.getLogger(__name__)

HOST_PROPS = ["name", "parent", "runtime.connectionState", "runtime.inMaintenanceMode"]
DATASTORE_PROPS = ["name", "summary.capacity", "summary.freeSpace"]
VM_PROPS = [
    "name", "parent", "config.annotation", "config.template", "config.hardware.numCPU", "config.hardware.memoryMB",
    "config.hardware.device", "config.files.vmPathName", "config.guestFullName", "guest.guestFullName",
    "runtime.powerState", "runtime.connectionState", "runtime.host", "runtime.bootTime", "layoutEx",
]

PC = vmodl.query.PropertyCollector
CONNECT_TIMEOUT_S = 120  # socket timeout; without it a stuck vCenter blocks the collector forever


def host_port(host: str) -> tuple[str, int]:
    name, sep, port = host.rpartition(":")
    return (name, int(port)) if sep and port.isdigit() else (host, 443)


def plain(value):
    """Managed object references become moref strings; everything else is returned as is."""
    return value._moId if isinstance(value, ManagedObject) else value


def to_disks(devices) -> list[dict]:
    disks = []
    for d in devices or []:
        if not isinstance(d, vim.vm.device.VirtualDisk):
            continue
        backing = d.backing
        ds = getattr(backing, "datastore", None)
        disks.append({
            "key": d.key,
            "label": d.deviceInfo.label if d.deviceInfo else "",
            "capacity_bytes": int(d.capacityInBytes or 0),
            "datastore": ds._moId if ds is not None else None,
            "file_name": getattr(backing, "fileName", "") or "",
        })
    return disks


def to_layout(layout) -> dict | None:
    if layout is None:
        return None
    return {
        "files": {f.key: int(f.size or 0) for f in layout.file or []},
        "disks": {d.key: [k for unit in d.chain or [] for k in unit.fileKey or []] for d in layout.disk or []},
    }


CONVERT = {"config.hardware.device": to_disks, "layoutEx": to_layout}


def connect(settings: CollectorSettings):
    host, port = host_port(settings.vcenter_host)
    return SmartConnect(host=host, port=port, user=settings.vcenter_user,
                        pwd=settings.vcenter_password.get_secret_value(),
                        disableSslCertValidation=not settings.vcenter_verify_ssl,
                        httpConnectionTimeout=CONNECT_TIMEOUT_S)


def disconnect(si) -> None:
    try:
        Disconnect(si)
    except Exception:  # noqa: BLE001 - logging out is best effort
        log.debug("disconnect failed", exc_info=True)


def _collect(si, root, obj_type, paths: list[str], extra=None) -> dict[str, dict]:
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(root, [obj_type], True)
    try:
        spec = PC.FilterSpec(
            objectSet=[PC.ObjectSpec(obj=view, skip=True, selectSet=[
                PC.TraversalSpec(name="view", path="view", skip=False, type=vim.view.ContainerView)])],
            propSet=[PC.PropertySpec(type=obj_type, pathSet=paths)],
        )
        pc = content.propertyCollector
        out: dict[str, dict] = {}
        result = pc.RetrievePropertiesEx([spec], PC.RetrieveOptions(maxObjects=1000))
        while result:
            for oc in result.objects:
                props = {p.name: CONVERT.get(p.name, plain)(p.val) for p in oc.propSet or []}
                if extra:
                    props.update(extra(oc.obj))
                out[oc.obj._moId] = props
            result = pc.ContinueRetrievePropertiesEx(result.token) if result.token else None
        return out
    finally:
        view.Destroy()


def find_datacenter(si, name: str):
    """Find a datacenter by name anywhere in the inventory (datacenters can sit inside folders)."""
    found = {moref: props for moref, props in
             _collect(si, si.RetrieveContent().rootFolder, vim.Datacenter, ["name", "vmFolder"]).items()
             if props.get("name") == name}
    if not found:
        raise ValueError(f"Datacenter not found: {name}")
    moref = next(iter(found))
    return vim.Datacenter(moref, si._stub), {moref: found[moref]}


def fetch_raw(si, settings: CollectorSettings) -> RawInventory:
    content = si.RetrieveContent()
    if settings.vcenter_datacenter:
        root, datacenters = find_datacenter(si, settings.vcenter_datacenter)
    else:
        root = content.rootFolder
        datacenters = _collect(si, root, vim.Datacenter, ["name", "vmFolder"])
    raw = RawInventory(
        vcenter_host=settings.vcenter_host,
        datacenters=datacenters,
        folders=_collect(si, root, vim.Folder, ["name", "parent"]),
        compute=_collect(si, root, vim.ComputeResource, ["name"],
                         extra=lambda o: {"is_cluster": isinstance(o, vim.ClusterComputeResource)}),
        hosts=_collect(si, root, vim.HostSystem, HOST_PROPS),
        datastores=_collect(si, root, vim.Datastore, DATASTORE_PROPS),
        vms=_collect(si, root, vim.VirtualMachine, VM_PROPS),
    )
    log.info("fetched %d hosts, %d VMs, %d datastores", len(raw.hosts), len(raw.vms), len(raw.datastores))
    return raw
