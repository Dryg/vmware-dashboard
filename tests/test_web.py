import gzip
import sqlite3
import statistics
from datetime import timedelta
from pathlib import Path

import pytest
from factories import NOW, make_inventory, make_vm
from fastapi.testclient import TestClient

from vmdash.config import WebSettings
from vmdash.db import connect_rw, finish_run, start_run, write_snapshot
from vmdash.mock.generate import generate_db
from vmdash.models import Tag
from vmdash.web.app import create_app
from vmdash.web.views import GROUPS

STATIC = Path(__file__).parent.parent / "src" / "vmdash" / "web" / "static"
AUTH = ("u", "p")


def make_client(db: Path, now=NOW) -> TestClient:
    settings = WebSettings(web_user="u", web_password="p", sqlite_path=db, _env_file=None)
    return TestClient(create_app(settings, now=lambda: now))


@pytest.fixture(scope="module")
def mock_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("web") / "m.db"
    generate_db(path, now=NOW)
    return path


@pytest.fixture
def client(mock_db):
    return make_client(mock_db)


def get(client, url="/", **kw):
    return client.get(url, auth=AUTH, **kw)


def test_requires_auth(client):
    r = client.get("/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


def test_wrong_password_401(client):
    assert client.get("/", auth=("u", "nope")).status_code == 401
    assert client.get("/", auth=("x", "p")).status_code == 401


def test_page_renders(client):
    r = get(client)
    assert r.status_code == 200
    assert "esx01.lab.local" in r.text
    assert 'data-snapshot="12"' in r.text
    assert "vcsa01.lab.local · DC-Main" in r.text
    assert "Synced 22.10.2026 12:00" in r.text
    assert '<html lang="en" data-theme="light">' in r.text


def test_hosts_in_natural_order_no_host_last(client):
    text = get(client).text
    positions = [text.index(f'data-key="{k}"') for k in
                 ("esx01.lab.local", "esx02.lab.local", "esx06.lab.local", "No host")]
    assert positions == sorted(positions)


@pytest.mark.parametrize("group", list(GROUPS))
def test_all_groups_render(client, group):
    r = get(client, f"/?group={group}")
    assert r.status_code == 200
    assert f'href="?group={group}" aria-current="page"' in r.text


def test_unknown_group_falls_back_to_host(client):
    r = get(client, "/?group=evil")
    assert r.status_code == 200
    assert 'href="?group=host" aria-current="page"' in r.text
    assert 'data-key="esx01.lab.local"' in r.text


def test_host_column_only_when_not_grouped_by_host(client):
    assert "<th>Host</th>" not in get(client, "/?group=host").text
    assert "<th>Host</th>" in get(client, "/?group=folder").text


def test_templates_marked(client):
    assert 'class="vm tpl"' in get(client).text


def test_html_escaping(tmp_path):
    conn = connect_rw(tmp_path / "x.db")
    rid = start_run(conn, now=NOW)
    finish_run(conn, rid, "success", now=NOW)
    write_snapshot(conn, make_inventory(vms=[make_vm("<script>x</script>", description='a "b"\nc',
                                                                tags=(Tag("<i>", "x&y"),))]), rid, 15)
    conn.close()
    text = get(make_client(tmp_path / "x.db")).text
    assert "<script>x" not in text
    assert "&lt;script&gt;x&lt;/script&gt;" in text
    assert '<span class="tag"><i>&lt;i&gt;</i>x&amp;y</span>' in text
    assert 'title="a &#34;b&#34;\nc"' in text or 'title="a &quot;b&quot;\nc"' in text


def test_no_db_renders_empty_state(tmp_path):
    r = get(make_client(tmp_path / "missing.db"))
    assert r.status_code == 200
    assert "No sync yet" in r.text
    assert not (tmp_path / "missing.db").exists()


def test_old_schema_renders_empty_state(tmp_path):
    sqlite3.connect(tmp_path / "old.db").close()
    r = get(make_client(tmp_path / "old.db"))
    assert r.status_code == 200
    assert "No sync yet" in r.text


def test_failed_sync_shows_banner(tmp_path):
    generate_db(tmp_path / "f.db", vms=10, now=NOW)
    conn = connect_rw(tmp_path / "f.db")
    rid = start_run(conn, now=NOW + timedelta(minutes=15))
    finish_run(conn, rid, "failed", error="ConnectionError: unreachable", now=NOW + timedelta(minutes=16))
    conn.close()
    text = get(make_client(tmp_path / "f.db", now=NOW + timedelta(minutes=17))).text
    assert "Sync failed 12:16" in text
    assert "Last sync failed at 22.10.2026 12:16: ConnectionError: unreachable. Showing data from 12:00." in text


def test_status_json(client):
    data = get(client, "/status.json").json()
    assert data["snapshot_id"] == 12
    assert set(data["last_run"]) == {"status", "started_at", "finished_at", "error"}
    assert data["last_run"]["status"] == "success"
    assert data["last_success_at"] == "2026-10-22T12:00:00+00:00"
    assert data["interval_minutes"] == 15
    assert data["badge"]["state"] == "ok"


def test_status_json_requires_auth(client):
    assert client.get("/status.json").status_code == 401


def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "last_success_age_s": 0}


def test_healthz_stale_503(mock_db):
    r = make_client(mock_db, now=NOW + timedelta(minutes=46)).get("/healthz")
    assert r.status_code == 503
    assert r.json()["ok"] is False


def test_healthz_no_db_503(tmp_path):
    assert make_client(tmp_path / "missing.db").get("/healthz").status_code == 503


def test_gzip(client):
    r = get(client, headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"


def test_page_budget_500_vms(tmp_path):
    generate_db(tmp_path / "big.db", hosts=10, vms=500, now=NOW)
    c = make_client(tmp_path / "big.db")
    get(c)  # warm up template cache
    durations = []
    for group in GROUPS:  # first render of each grouping, inventory cache warm
        r = get(c, f"/?group={group}", headers={"Accept-Encoding": "gzip"})
        durations.append(float(r.headers["server-timing"].split("dur=")[1]))
    assert len(gzip.compress(r.content)) < 100_000
    assert statistics.median(durations) < 50, durations


def test_server_timing_header(client):
    assert get(client, "/healthz").headers["server-timing"].startswith("app;dur=")


def test_static_sizes():
    assert (STATIC / "style.css").stat().st_size < 8192


def test_static_cache_headers(client):
    page = get(client).text
    assert "/static/style.css?v=" in page
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert "max-age=31536000" in r.headers["cache-control"]


def test_no_api_docs(client):
    assert client.get("/docs").status_code == 404


def test_new_snapshot_is_shown(tmp_path):
    generate_db(tmp_path / "s.db", vms=10, now=NOW)
    c = make_client(tmp_path / "s.db")
    assert "brand-new-vm" not in get(c).text
    conn = connect_rw(tmp_path / "s.db")
    rid = start_run(conn, now=NOW)
    finish_run(conn, rid, "success", now=NOW)
    write_snapshot(conn, make_inventory(vms=[make_vm("brand-new-vm")]), rid, 15)
    conn.close()
    assert "brand-new-vm" in get(c).text


def test_app_js_size():
    assert (STATIC / "app.js").stat().st_size < 8192


def test_page_loads_app_js(client):
    assert '<script src="/static/app.js?v=' in get(client).text
