"""vSphere Automation REST client for VM tags (vSphere 7.0U2+ /api endpoints)."""

import logging
import ssl
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import httpx

from vmdash.config import https_url
from vmdash.models import Tag

log = logging.getLogger(__name__)

BATCH = 200
WORKERS = 8  # parallel GETs for category and tag details; the API has no batch read for them


class TagError(Exception):
    """Tag lookup failed. The message never contains credentials or session tokens."""


def _check(r: httpx.Response) -> httpx.Response:
    if r.is_error:
        raise TagError(f"{r.request.method} {r.request.url.path} returned HTTP {r.status_code}")
    return r


def fetch_vm_tags(host: str, user: str, password: str, verify: ssl.SSLContext | bool,
                  transport: httpx.BaseTransport | None = None, timeout: float = 30.0) -> dict[str, list[Tag]]:
    """Return {vm moref: [Tag, ...]} sorted by (category, name)."""
    try:
        with httpx.Client(base_url=https_url(host), verify=verify, timeout=timeout, transport=transport) as client:
            token = _check(client.post("/api/session", auth=(user, password))).json()
            client.headers["vmware-api-session-id"] = token
            try:
                return _collect(client)
            finally:
                try:
                    client.delete("/api/session")
                except httpx.HTTPError:
                    pass
    except httpx.HTTPError as e:
        raise TagError(f"{type(e).__name__} talking to {host}") from None
    except ValueError as e:  # bad JSON
        raise TagError(f"unexpected response from {host}: {type(e).__name__}") from None


def _details(client: httpx.Client, pool: ThreadPoolExecutor, kind: str) -> list[dict]:
    ids = _check(client.get(f"/api/cis/tagging/{kind}")).json()
    return list(pool.map(lambda i: _check(client.get(f"/api/cis/tagging/{kind}/{quote(i, safe=':')}")).json(), ids))


def _collect(client: httpx.Client) -> dict[str, list[Tag]]:
    with ThreadPoolExecutor(WORKERS) as pool:
        categories = {c["id"]: c["name"] for c in _details(client, pool, "category")}
        tags = {t["id"]: Tag(categories.get(t["category_id"], ""), t["name"]) for t in _details(client, pool, "tag")}
    if not tags:
        return {}

    by_vm: dict[str, list[Tag]] = {}
    ids = list(tags)
    for i in range(0, len(ids), BATCH):
        r = _check(client.post("/api/cis/tagging/tag-association",
                               params={"action": "list-attached-objects-on-tags"},
                               json={"tag_ids": ids[i:i + BATCH]}))
        for item in r.json():
            for obj in item.get("object_ids", []):
                if obj.get("type") == "VirtualMachine":
                    by_vm.setdefault(obj["id"], []).append(tags[item["tag_id"]])
    for vm_tags in by_vm.values():
        vm_tags.sort(key=lambda t: (t.category, t.name))
    log.debug("tags: %d categories, %d tags, %d tagged VMs", len(categories), len(tags), len(by_vm))
    return by_vm
