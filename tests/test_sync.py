import pytest

from vmdash.collector.sync import run_sync
from vmdash.collector.tags import TagError
from vmdash.collector.transform import RawInventory
from vmdash.config import CollectorSettings
from vmdash.db import connect_rw
from vmdash.models import Tag


@pytest.fixture
def settings(tmp_path):
    return CollectorSettings(vcenter_host="vc", vcenter_user="u", vcenter_password="s3cr3t-pw",
                             sqlite_path=tmp_path / "a.db", _env_file=None)


@pytest.fixture
def conn(settings):
    c = connect_rw(settings.sqlite_path)
    yield c
    c.close()


def fake_raw(_settings, name="web01"):
    return RawInventory(
        vcenter_host="vc",
        datacenters={"datacenter-1": {"name": "DC-Main", "vmFolder": "group-v1"}},
        hosts={"host-1": {"name": "esx01", "parent": None, "runtime.connectionState": "connected"}},
        vms={"vm-1": {"name": name, "runtime.host": "host-1", "runtime.powerState": "poweredOn"}},
    )


def no_tags(_settings):
    return {}


def boom(_settings):
    raise ConnectionError("unreachable")


def last_run(conn):
    return conn.execute("select * from sync_runs order by id desc limit 1").fetchone()


def meta(conn):
    return dict(conn.execute("select key, value from meta").fetchall())


def test_success_writes_snapshot(conn, settings):
    assert run_sync(settings, conn, fetch=fake_raw, fetch_tags=lambda s: {"vm-1": [Tag("env", "prod")]}) == "success"
    assert [r[0] for r in conn.execute("select name from vms")] == ["web01"]
    assert [tuple(r) for r in conn.execute("select category, name from tags")] == [("env", "prod")]
    run = last_run(conn)
    assert (run["status"], run["host_count"], run["vm_count"], run["error"]) == ("success", 1, 1, None)
    assert meta(conn)["snapshot_id"] == str(run["id"])
    assert meta(conn)["sync_interval_minutes"] == "15"


def test_tag_failure_is_partial(conn, settings):
    def bad_tags(_s):
        raise TagError("POST /api/session returned HTTP 401")
    assert run_sync(settings, conn, fetch=fake_raw, fetch_tags=bad_tags) == "partial"
    assert conn.execute("select count(*) from vms").fetchone()[0] == 1
    assert conn.execute("select count(*) from tags").fetchone()[0] == 0
    assert last_run(conn)["error"] == "tags: TagError: POST /api/session returned HTTP 401"


def test_unexpected_tag_exception_is_partial(conn, settings):
    def bad_tags(_s):
        raise KeyError("name")
    assert run_sync(settings, conn, fetch=fake_raw, fetch_tags=bad_tags) == "partial"


def test_fetch_failure_keeps_old_snapshot(conn, settings):
    run_sync(settings, conn, fetch=fake_raw, fetch_tags=no_tags)
    first = last_run(conn)["id"]
    assert run_sync(settings, conn, fetch=boom, fetch_tags=no_tags) == "failed"
    assert [r[0] for r in conn.execute("select name from vms")] == ["web01"]
    run = last_run(conn)
    assert (run["status"], run["error"]) == ("failed", "ConnectionError: unreachable")
    assert run["finished_at"] is not None
    assert meta(conn)["snapshot_id"] == str(first)


def test_error_never_contains_password(conn, settings):
    def leaky(_s):
        raise Exception("login failed for u with s3cr3t-pw")
    run_sync(settings, conn, fetch=leaky, fetch_tags=no_tags)
    assert last_run(conn)["error"] == "Exception: login failed for u with ***"


def test_error_truncated(conn, settings):
    def long(_s):
        raise RuntimeError("x" * 2000)
    run_sync(settings, conn, fetch=long, fetch_tags=no_tags)
    assert len(last_run(conn)["error"]) == 500


def test_vmodl_fault_error_is_one_line_message(conn, settings):
    from pyVmomi import vim

    def bad_login(_s):
        raise vim.fault.InvalidLogin(msg="Cannot complete login due to an incorrect user name or password.")
    run_sync(settings, conn, fetch=bad_login, fetch_tags=no_tags)
    assert last_run(conn)["error"] == (
        "vim.fault.InvalidLogin: Cannot complete login due to an incorrect user name or password.")


def test_multiline_error_collapsed(conn, settings):
    def multi(_s):
        raise RuntimeError("line one\n   line two\n")
    run_sync(settings, conn, fetch=multi, fetch_tags=no_tags)
    assert last_run(conn)["error"] == "RuntimeError: line one line two"
