import json

import httpx
import pytest

from vmdash.collector.tags import TagError, fetch_vm_tags
from vmdash.models import Tag

CAT_ENV, CAT_TEAM = "urn:vmomi:InventoryServiceCategory:env:GLOBAL", "urn:vmomi:InventoryServiceCategory:team:GLOBAL"
TAG_PROD, TAG_DEV, TAG_WEB = "urn:t:prod", "urn:t:dev", "urn:t:web"


def vcenter(tags=True, fail=None, calls=None):
    """A fake vCenter REST API; `fail` maps (method, path) to a status code."""
    categories = {CAT_ENV: "env", CAT_TEAM: "team"} if tags else {}
    tag_defs = {TAG_PROD: ("prod", CAT_ENV), TAG_DEV: ("dev", CAT_ENV), TAG_WEB: ("web", CAT_TEAM)} if tags else {}
    attached = {TAG_PROD: [{"id": "vm-1", "type": "VirtualMachine"}, {"id": "host-1", "type": "HostSystem"}],
                TAG_DEV: [{"id": "vm-2", "type": "VirtualMachine"}],
                TAG_WEB: [{"id": "vm-1", "type": "VirtualMachine"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if calls is not None:
            calls.append(key)
        if fail and key in fail:
            return httpx.Response(fail[key], json={"error_type": "UNAUTHENTICATED"})
        if key == ("POST", "/api/session"):
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(201, json="token-123")
        assert request.headers.get("vmware-api-session-id") == "token-123"
        path = request.url.path
        if key == ("DELETE", "/api/session"):
            return httpx.Response(204)
        if path == "/api/cis/tagging/category":
            return httpx.Response(200, json=list(categories))
        if path.startswith("/api/cis/tagging/category/"):
            cid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": cid, "name": categories[cid]})
        if path == "/api/cis/tagging/tag":
            return httpx.Response(200, json=list(tag_defs))
        if path.startswith("/api/cis/tagging/tag/"):
            tid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": tid, "name": tag_defs[tid][0], "category_id": tag_defs[tid][1]})
        if path == "/api/cis/tagging/tag-association":
            assert request.url.params["action"] == "list-attached-objects-on-tags"
            ids = json.loads(request.content)["tag_ids"]
            return httpx.Response(200, json=[{"tag_id": t, "object_ids": attached[t]} for t in ids])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def fetch(transport):
    return fetch_vm_tags("vc", "u", "secret", True, transport=transport)


def test_maps_tags_to_vms():
    assert fetch(vcenter()) == {"vm-1": [Tag("env", "prod"), Tag("team", "web")], "vm-2": [Tag("env", "dev")]}


def test_ignores_non_vm_objects():
    assert "host-1" not in fetch(vcenter())


def test_no_tags_skips_association_call():
    calls = []
    assert fetch(vcenter(tags=False, calls=calls)) == {}
    assert ("POST", "/api/cis/tagging/tag-association") not in calls


def test_auth_failure_raises_tag_error():
    with pytest.raises(TagError) as err:
        fetch(vcenter(fail={("POST", "/api/session"): 401}))
    assert "401" in str(err.value)
    assert "secret" not in str(err.value)


def test_api_error_mid_way_raises_and_deletes_session():
    calls = []
    with pytest.raises(TagError) as err:
        fetch(vcenter(fail={("GET", "/api/cis/tagging/tag"): 500}, calls=calls))
    assert "token-123" not in str(err.value)
    assert calls[-1] == ("DELETE", "/api/session")


def test_session_deleted_at_end():
    calls = []
    fetch(vcenter(calls=calls))
    assert calls[0] == ("POST", "/api/session")
    assert calls[-1] == ("DELETE", "/api/session")


def test_network_error_raises_tag_error():
    def boom(request):
        raise httpx.ConnectError("connection refused")
    with pytest.raises(TagError):
        fetch(httpx.MockTransport(boom))
