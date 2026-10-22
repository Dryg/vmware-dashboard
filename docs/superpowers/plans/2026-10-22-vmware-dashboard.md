# vmdash Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build vmdash: a collector daemon that snapshots a vSphere 8 inventory into SQLite, and a fast server-rendered web page that displays it.

**Architecture:** One Python package `vmdash` with two processes sharing one SQLite file (WAL). `vmdash-collector` is the only writer (pyVmomi + vSphere REST tagging). `vmdash-web` (FastAPI + Jinja2) opens the DB read-only and renders HTML; a small vanilla JS file adds search, collapse, theme and auto-refresh.

**Tech Stack:** Python 3.12+, uv, FastAPI, uvicorn, Jinja2, pydantic-settings, pyVmomi, httpx, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-22-vmware-dashboard-design.md` (read it first). Visual reference: `docs/design/mockup.html` — the web page must match it.

## Global Constraints

- Python `>=3.12`; run everything through `uv run`.
- Runtime deps only: `fastapi`, `uvicorn`, `jinja2`, `pydantic-settings`, `pyvmomi`, `httpx`. Dev: `pytest`, `ruff`. No JS/CSS libraries, no web fonts, no build step.
- The web process never imports `pyVmomi` or contacts vCenter; it opens SQLite with `mode=ro`.
- All stored timestamps are ISO 8601 UTC strings (`datetime.isoformat()` of tz-aware UTC values). Displayed as `DD.MM.YYYY HH:MM` in server local time.
- `PRAGMA user_version = 1` (`SCHEMA_VERSION = 1`).
- Passwords and session tokens never appear in logs or error messages.
- Page: under 100 KB gzipped and under 50 ms server time at 500 VMs. `app.js` and `style.css` each under 8 KB.
- UI copy, column names, group names and order exactly as spec §7.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Hostile or odd text from vCenter** (`<script>` in a VM name, quotes and newlines in annotations, unicode names) must render escaped and still be searchable. Test: Task 5 `test_html_escaping`.
2. **Web started before the collector ever ran, or DB from an older schema** must show "No sync yet" with HTTP 200, never 500. Test: Task 5 `test_no_db_renders_empty_state`, `test_old_schema_renders_empty_state`.
3. **Collector killed mid-sync or vCenter unreachable** must keep the previous snapshot and leave a `failed` run. Tests: Task 2 `test_fail_stale_runs`, Task 10 `test_fetch_failure_keeps_old_snapshot`.
4. **VM with missing data** (inaccessible VM: no `config`, no `layoutEx`, disk with no layout chain, no boot time) must still be listed with zeros/blanks. Test: Task 7 `test_inaccessible_vm_is_kept`.
5. **Unknown `?group=` value** falls back to Host grouping, not 500. Test: Task 5 `test_unknown_group_falls_back_to_host`.

---

## File Map

| File | Responsibility |
|---|---|
| `pyproject.toml`, `.env.example`, `README.md` | project, entry points, config docs |
| `src/vmdash/config.py` | settings classes, CLI args `--env-file` / `--db` |
| `src/vmdash/log.py` | logging setup |
| `src/vmdash/models.py` | dataclasses shared by collector, DB, web, mock |
| `src/vmdash/db.py` | schema, connections, writes (snapshot, sync_runs) |
| `src/vmdash/mock/generate.py` | mock Inventory + DB |
| `src/vmdash/web/queries.py` | SQLite → `Inventory`, `SyncStatus` |
| `src/vmdash/web/views.py` | pure view logic: groups, status, uptime, badge, totals |
| `src/vmdash/web/app.py` | FastAPI app, auth, routes, `main()` |
| `src/vmdash/web/templates/index.html`, `static/style.css`, `static/app.js` | page |
| `src/vmdash/collector/transform.py` | raw dicts → `Inventory` (pure) |
| `src/vmdash/collector/tags.py` | REST tagging client |
| `src/vmdash/collector/vcenter.py` | pyVmomi connect + fetch → `RawInventory` |
| `src/vmdash/collector/sync.py` | one sync run |
| `src/vmdash/collector/main.py` | daemon loop, `main()` |
| `deploy/systemd/*.service`, `deploy/INSTALL.md` | deployment |

---

### Task 1: Project scaffold, config, logging

**Files:**
- Create: `pyproject.toml`, `.env.example`, `src/vmdash/__init__.py`, `src/vmdash/config.py`, `src/vmdash/log.py`, empty `__init__.py` in `src/vmdash/{collector,web,mock}/`
- Test: `tests/test_config.py`, `tests/test_log.py`

**Interfaces:**
- Produces:
  - `class BaseConfig(BaseSettings)`: `sqlite_path: Path = Path("vmdash.db")`, `log_level: str = "INFO"`, `log_file: Path | None = None`; `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`; empty string env values treated as unset (`env_ignore_empty=True`).
  - `class CollectorSettings(BaseConfig)`: `vcenter_host: str`, `vcenter_user: str`, `vcenter_password: SecretStr`, `vcenter_verify_ssl: bool = True`, `vcenter_datacenter: str = ""`, `sync_interval_minutes: int = Field(15, ge=1)`.
  - `class WebSettings(BaseConfig)`: `web_host: str = "0.0.0.0"`, `web_port: int = 8080`, `web_user: str`, `web_password: SecretStr`, `web_ssl_cert: Path | None = None`, `web_ssl_key: Path | None = None`.
  - `add_common_args(parser: argparse.ArgumentParser) -> None` — adds `--env-file PATH` and `--db PATH`.
  - `load_settings(cls: type[T], args: argparse.Namespace) -> T` — `cls(_env_file=args.env_file or ".env")`, then `sqlite_path` replaced by `args.db` when given.
  - `setup_logging(level: str, file: Path | None) -> None` in `log.py`.

- [ ] **Step 1: Create `pyproject.toml`**

hatchling build backend, `src/` layout, `requires-python = ">=3.12"`, deps from Global Constraints, `[dependency-groups] dev = ["pytest>=8", "ruff>=0.7"]`, scripts:
`vmdash-collector = "vmdash.collector.main:main"`, `vmdash-web = "vmdash.web.app:main"`, `vmdash-mockdb = "vmdash.mock.generate:main"`. `[tool.pytest.ini_options] testpaths = ["tests"]`. Run `uv sync`; expected: lockfile created, no errors.

- [ ] **Step 2: Write failing tests** (`monkeypatch.chdir(tmp_path)` so no real `.env` leaks in)

```python
def test_collector_settings_from_env_file(tmp_path, monkeypatch):
    (tmp_path / "x.env").write_text("VCENTER_HOST=vc\nVCENTER_USER=u\nVCENTER_PASSWORD=p\nSQLITE_PATH=/data/a.db\nVCENTER_DATACENTER=\n")
    args = parse(["--env-file", str(tmp_path / "x.env")])
    s = load_settings(CollectorSettings, args)
    assert s.vcenter_host == "vc" and s.sqlite_path == Path("/data/a.db")
    assert s.vcenter_datacenter == "" and s.sync_interval_minutes == 15 and s.vcenter_verify_ssl is True

def test_db_flag_overrides_env(...):  # --db /tmp/b.db wins over SQLITE_PATH
def test_missing_required_raises(...):  # empty env → pydantic ValidationError for WebSettings
def test_password_not_in_repr(...):     # "p" not in repr(settings)
def test_interval_must_be_positive(...): # SYNC_INTERVAL_MINUTES=0 → ValidationError
```
`tests/test_log.py`: `test_file_handler_writes(tmp_path)` — `setup_logging("DEBUG", tmp_path/"v.log")`, log a line, flush handlers, assert line in file; `test_level_applied` — root level is `logging.WARNING` after `setup_logging("WARNING", None)`.

- [ ] **Step 3: Run** `uv run pytest tests/test_config.py tests/test_log.py -v` — expected FAIL (ImportError).

- [ ] **Step 4: Implement `config.py` and `log.py`**

`setup_logging`: format `%(asctime)s %(levelname)s %(name)s: %(message)s`; replaces root handlers (idempotent); stdout `StreamHandler`; `RotatingFileHandler(file, maxBytes=10_000_000, backupCount=5)` when `file`; sets `uvicorn`, `uvicorn.error`, `uvicorn.access` loggers to `propagate=True` with no own handlers; sets `urllib3` logger to WARNING.

- [ ] **Step 5: Create `.env.example`** with every variable from spec §4, defaults filled, secrets as `change-me`, one comment line each.

- [ ] **Step 6: Run tests** — expected PASS. Run `uv run ruff check src tests` — expected clean.

- [ ] **Step 7: Commit** `feat: project scaffold, settings and logging`

---

### Task 2: Models and database layer

**Files:**
- Create: `src/vmdash/models.py`, `src/vmdash/db.py`
- Test: `tests/test_db.py`, `tests/factories.py` (helpers `make_vm(**kw) -> Vm`, `make_inventory(**kw) -> Inventory` with sensible defaults, reused by later tasks)

**Interfaces:**
- Produces (`models.py`, all `@dataclass(frozen=True, slots=True)`):
  - `Host(moref: str, name: str, cluster: str | None, connection_state: str, in_maintenance: bool)`
  - `Datastore(moref: str, name: str, capacity_bytes: int, free_bytes: int)`
  - `Disk(key: int, label: str, capacity_bytes: int, used_bytes: int, datastore: str)`
  - `Tag(category: str, name: str)`
  - `Vm(moref, name, host_moref: str | None, description: str, folder_path: str, power_state: str, connection_state: str, is_template: bool, vcpu: int, memory_mb: int, guest_os: str, boot_time: datetime | None, datastore_path: str, disks: tuple[Disk, ...] = (), tags: tuple[Tag, ...] = ())`
  - `Inventory(vcenter_host: str, datacenter: str, hosts: tuple[Host, ...], datastores: tuple[Datastore, ...], vms: tuple[Vm, ...])`
- Produces (`db.py`):
  - `SCHEMA_VERSION = 1`
  - `connect_rw(path: Path) -> sqlite3.Connection` — creates parent dir, WAL, `foreign_keys` off, `row_factory = sqlite3.Row`; creates schema; if `user_version` not in (0, 1) drops all tables and recreates.
  - `connect_ro(path: Path) -> sqlite3.Connection | None` — `None` if file missing or `user_version != SCHEMA_VERSION`; URI `file:{path}?mode=ro`.
  - `start_run(conn, now: datetime | None = None) -> int`
  - `finish_run(conn, run_id: int, status: str, error: str | None = None, host_count: int = 0, vm_count: int = 0, now: datetime | None = None) -> None` — also prunes `sync_runs` to newest 500.
  - `fail_stale_runs(conn) -> int` — sets `running` rows to `failed`, error `"collector stopped during sync"`; returns count.
  - `write_snapshot(conn, inv: Inventory, run_id: int, interval_minutes: int) -> None` — single transaction: delete all rows from `hosts, datastores, vms, disks, tags, meta`, insert everything, meta keys `vcenter_host`, `datacenter`, `sync_interval_minutes`, `snapshot_id` (= `run_id`).

- [ ] **Step 1: Write failing tests**

```python
def test_schema_created_with_version(tmp_path):
    conn = connect_rw(tmp_path / "a.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

def test_write_snapshot_replaces_previous(tmp_path):
    # write inventory with vms a,b; then with vm c only → vms table has only c; disks/tags of a,b gone
def test_snapshot_meta(tmp_path):
    # meta has vcenter_host, datacenter, sync_interval_minutes == "15", snapshot_id == str(run_id)
def test_boot_time_round_trip_is_utc_iso(tmp_path):
    # Vm with boot_time=datetime(2026,10,1,8,0,tzinfo=UTC) → stored "2026-10-01T08:00:00+00:00"
def test_fail_stale_runs(tmp_path):
    rid = start_run(conn); assert fail_stale_runs(conn) == 1
    row = conn.execute("select status, error from sync_runs where id=?", (rid,)).fetchone()
    assert tuple(row) == ("failed", "collector stopped during sync")
def test_runs_pruned_to_500(tmp_path):  # 505 start/finish → count == 500, oldest ids gone
def test_old_schema_version_is_recreated(tmp_path):  # set user_version=99, add junk table → connect_rw → version 1, junk gone
def test_connect_ro_missing_file_returns_none(tmp_path)
def test_connect_ro_wrong_version_returns_none(tmp_path)
def test_connect_ro_cannot_write(tmp_path):  # insert raises sqlite3.OperationalError
```

- [ ] **Step 2: Run** `uv run pytest tests/test_db.py -v` — expected FAIL.
- [ ] **Step 3: Implement `models.py` and `db.py`** — schema exactly spec §5 plus `meta`; index on `disks(vm_moref)`, `tags(vm_moref)`. `now` defaults to `datetime.now(UTC)`.
- [ ] **Step 4: Run tests** — expected PASS.
- [ ] **Step 5: Commit** `feat: data models and SQLite layer`

---

### Task 3: Mock database generator

**Files:**
- Create: `src/vmdash/mock/generate.py`
- Test: `tests/test_mock.py`

**Interfaces:**
- Consumes: `models.*`, `db.connect_rw`, `db.start_run`, `db.finish_run`, `db.write_snapshot`.
- Produces:
  - `generate_inventory(hosts: int = 6, vms: int = 80, seed: int = 42, now: datetime | None = None) -> Inventory`
  - `generate_db(path: Path, hosts: int = 6, vms: int = 80, seed: int = 42, now: datetime | None = None) -> None` — deletes existing file first; writes 12 sync runs 15 min apart ending at `now`, the 4th-newest `failed` with error `"Cannot connect to vcsa01.lab.local:443 (timed out after 30 s)"`, newest `success` wrote the snapshot.
  - `main(argv: list[str] | None = None) -> None` — args `--db` (default `mock.db`), `--hosts`, `--vms`, `--seed`.

- [ ] **Step 1: Write failing tests** (fixed `NOW = datetime(2026,10,22,12,0,tzinfo=UTC)`)

```python
def test_deterministic():
    assert generate_inventory(seed=1, now=NOW) == generate_inventory(seed=1, now=NOW)
    assert generate_inventory(seed=1, now=NOW) != generate_inventory(seed=2, now=NOW)

def test_counts():
    inv = generate_inventory(hosts=6, vms=80, now=NOW)
    assert len(inv.hosts) == 6 and len(inv.vms) == 80

def test_covers_required_cases():
    inv = generate_inventory(now=NOW)
    assert any(h.in_maintenance for h in inv.hosts)
    assert any(h.connection_state == "disconnected" for h in inv.hosts)
    assert any(v.host_moref is None and v.connection_state == "orphaned" for v in inv.vms)
    assert any(v.is_template for v in inv.vms)
    assert {v.power_state for v in inv.vms} >= {"poweredOn", "poweredOff", "suspended"}
    assert {len(v.disks) for v in inv.vms} >= {1, 4}
    assert any(1 - d.free_bytes / d.capacity_bytes > 0.85 for d in inv.datastores)
    assert any("2012 R2" in v.guest_os for v in inv.vms)
    assert any(len(v.description) > 80 for v in inv.vms)
    assert len({t.category for v in inv.vms for t in v.tags}) >= 3
    # uptime buckets: boot ages <1d, 1–7d, 7–30d, 30–90d, >90d all present among poweredOn VMs

def test_generate_db_writes_runs(tmp_path):
    generate_db(tmp_path / "m.db", now=NOW)
    conn = connect_rw(tmp_path / "m.db")
    statuses = [r[0] for r in conn.execute("select status from sync_runs order by id")]
    assert len(statuses) == 12 and statuses[-1] == "success" and statuses.count("failed") == 1
```

- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** with `random.Random(seed)` only (no global random, no `uuid`). `vcenter_host="vcsa01.lab.local"`, `datacenter="DC-Main"`. Hosts `esx01.lab.local`…; clusters `Cluster-A`/`Cluster-B`; folders under Production/Infra/Dev/Archive/Templates/"Discovered virtual machine"; datastores `ds-ssd01..03`, `ds-hdd01..02`; tag categories `env`, `owner`, `team`, `backup`. Reuse names/descriptions style from `docs/design/mockup.html`. Force the required cases from the test onto specific indices so small `vms` counts still cover them.
- [ ] **Step 4: Run tests** — PASS. Manual: `uv run vmdash-mockdb --db mock.db` → file exists.
- [ ] **Step 5: Commit** `feat: mock database generator`

---

### Task 4: Web read layer and view logic

**Files:**
- Create: `src/vmdash/web/queries.py`, `src/vmdash/web/views.py`
- Test: `tests/test_queries.py`, `tests/test_views.py`

**Interfaces:**
- Consumes: `models.*`, `db.connect_ro`, `tests/factories.py`, `mock.generate.generate_db`.
- Produces (`queries.py`):
  - `RunInfo(id: int, status: str, started_at: datetime, finished_at: datetime | None, error: str | None)` (frozen dataclass)
  - `SyncStatus(snapshot_id: int | None, last_run: RunInfo | None, last_success_at: datetime | None, interval_minutes: int, vcenter_host: str, datacenter: str)` — `last_success_at` = `finished_at` of newest run with status `success` or `partial`; `interval_minutes` defaults to 15 when meta is missing.
  - `read_inventory(conn) -> Inventory`, `read_status(conn) -> SyncStatus`
- Produces (`views.py`):
  - `GROUPS: dict[str, str] = {"host": "Host", "folder": "Folder", "cluster": "Cluster", "datastore": "Datastore", "power": "Power state", "uptime": "Uptime", "os": "Guest OS"}`
  - `natural_key(s: str) -> tuple` — case-insensitive, digit runs compared as ints.
  - `display_status(vm: Vm) -> str` — rules in spec §7 "Display status".
  - `status_class(status: str) -> str` — `Running→ok, Suspended→warn, Shut down→off, Unknown→bad, Orphaned→bad, Template→tpl`.
  - `uptime_bucket(vm: Vm, now: datetime) -> str` — one of `"Under 1 day", "1–7 days", "7–30 days", "30–90 days", "Over 90 days", "Not running"` (en dash). Not running when status ≠ Running or `boot_time` is None.
  - `uptime_text(vm: Vm, now: datetime) -> str` — `"up 9 h"` under 1 day, `"up 23 d"` otherwise, `""` when not running.
  - `datastore_name(datastore_path: str) -> str` — `"[ds-ssd01] web01/" → "ds-ssd01"`, `""` if no brackets.
  - `VmRow(vm: Vm, host_name: str, cluster: str, status: str, status_class: str, uptime: str, total_cap: int, total_used: int, search: str)` — `search` is the lowercased join of: name, description, folder_path, `"cat:name"` per tag (and `"cat name"`), status, uptime, guest_os, host name, cluster, host state label, datastore_path, `f"{vcpu} vcpu"`, `f"{ram_gb} gb"`, each disk's formatted sizes.
  - `Totals(vms: int, running: int, vcpu: int, ram_gb: int, cap: int, used: int, hosts: int)`
  - `Group(key: str, label: str, rows: list[VmRow], totals: Totals, host: Host | None, datastore: Datastore | None)` — `totals` exclude templates.
  - `build_groups(inv: Inventory, group: str, now: datetime) -> list[Group]` — order per spec §7 table; unknown `group` → `"host"`; rows sorted by `natural_key(vm.name)`.
  - `summary(inv: Inventory) -> Totals` (templates excluded)
  - `Badge(state: str, line1: str, line2: str, banner: str | None)`; `badge(st: SyncStatus, now: datetime) -> Badge` — states `ok | running | failed | stale | none`; copy:
    - ok: `"Synced 22.10.2026 14:05"` / `"Next sync about 14:20 · page updates itself"`
    - running: `"Syncing… started 14:20"` / `"Showing data from 14:05"`
    - failed: `"Sync failed 14:20"` / `"Showing data from 14:05"`; banner `"Last sync failed at 22.10.2026 14:20: {error}. Showing data from 14:05."`
    - stale (last success older than 3× interval, checked after running/failed): `"Data is 52 min old"` / `"Last sync 13:28 · expected every 15 min"`; banner `"No successful sync for 52 minutes (expected every 15). Check the collector: systemctl status vmdash-collector"`
    - none (no `snapshot_id`): `"No sync yet"` / `"Waiting for the collector"`
  - `fmt_size(n_bytes: int) -> str` — `"60 GB"`, `"2 TB"`, `"1.7 TB"` (binary units, ≥1024 GB → TB with one decimal, trailing `.0` dropped); `fmt_time(dt) -> str` `"HH:MM"`, `fmt_datetime(dt) -> str` `"DD.MM.YYYY HH:MM"` (local time).

- [ ] **Step 1: Write failing tests for `views.py`** (build inventories with `tests/factories.py`)

```python
def test_hosts_natural_sorted_no_host_last():
    keys = [g.key for g in build_groups(inv_with_hosts("esx10", "esx2", "ESX1", None), "host", NOW)]
    assert keys == ["ESX1", "esx2", "esx10", "No host"]

def test_folder_group_uses_first_segment():  # "Production/Web" and "Production/DB" → one "Production" group
def test_power_group_order():  # ["Running","Suspended","Shut down","Unknown","Orphaned","Template"] filtered to present ones
def test_uptime_buckets():
    assert uptime_bucket(running(boot=NOW - timedelta(hours=5)), NOW) == "Under 1 day"
    assert uptime_bucket(running(boot=NOW - timedelta(days=95)), NOW) == "Over 90 days"
    assert uptime_bucket(make_vm(power_state="poweredOff"), NOW) == "Not running"
    assert uptime_text(running(boot=NOW - timedelta(hours=9, minutes=40)), NOW) == "up 9 h"
def test_display_status_rules():  # template wins; orphaned; disconnected → Unknown; suspended
def test_datastore_group_has_datastore_and_order()
def test_cluster_group_no_cluster_last()
def test_unknown_group_falls_back(): assert [g.key for g in build_groups(inv, "nope", NOW)] == [g.key for g in build_groups(inv, "host", NOW)]
def test_group_totals_exclude_templates()
def test_search_text_contains_everything():  # tag "env:prod", host name, cluster, guest os, "4 vcpu", "16 gb", "60 gb"
def test_fmt_size(): assert fmt_size(60 * 2**30) == "60 GB"; assert fmt_size(2048 * 2**30) == "2 TB"; assert fmt_size(int(1.74 * 2**40)) == "1.7 TB"
def test_badge_states():  # one assert per state: ok, running, failed (banner contains error), stale (3×interval+1 min), none
```

- [ ] **Step 2: Write failing tests for `queries.py`**: `test_read_inventory_round_trip` (write `make_inventory()` with `write_snapshot`, read back, equal), `test_read_status_from_mock_db` (generate_db with NOW → `snapshot_id` is newest run id, `last_run.status == "success"`, `interval_minutes == 15`, `vcenter_host == "vcsa01.lab.local"`), `test_last_success_skips_failed_run`.
- [ ] **Step 3: Run** `uv run pytest tests/test_views.py tests/test_queries.py -v` — FAIL.
- [ ] **Step 4: Implement `queries.py` then `views.py`.** `read_inventory` uses 4 queries total (hosts, datastores, vms, disks+tags) and assembles in Python — no per-VM queries.
- [ ] **Step 5: Run tests** — PASS.
- [ ] **Step 6: Commit** `feat: web read layer and view logic`

---

### Task 5: Web app, routes, template, CSS

**Files:**
- Create: `src/vmdash/web/app.py`, `src/vmdash/web/templates/index.html`, `src/vmdash/web/static/style.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `WebSettings`, `load_settings`, `add_common_args`, `setup_logging`, `connect_ro`, `read_inventory`, `read_status`, all of `views.py`.
- Produces: `create_app(settings: WebSettings, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> FastAPI`; `main(argv: list[str] | None = None) -> None` (runs uvicorn with optional SSL).
- Produces for Task 6 (DOM contract the JS relies on):
  - `<html data-theme="light">`; inline `<head>` script reads `localStorage["vmdash.theme"]` and sets `data-theme` before CSS paints.
  - `#q` search input, `#kbd` hint, `#tpl` checkbox, `#fold` button, `#count`, `#summary`, `#sync` badge (`.dot`, `.l1`, `.l2`), `#banner`, `#theme` button, `#t` table.
  - `<body data-snapshot="{id}" data-group="{group}">`.
  - One `<tbody class="grp" data-key="...">` per group; first row `tr.grow` with `button.gbtn` and `span.htot`.
  - VM rows `<tr class="vm[ tpl]" data-q="{search}" data-cpu data-ram data-cap data-used data-run="1|0" data-host="{host}">`.
  - Summary spans `[data-sum=hosts|vms|running|vcpu|ram|cap|used]`.

- [ ] **Step 1: Write failing tests** (fixture: `generate_db(tmp_path/"m.db", now=NOW)`; `create_app(WebSettings(web_user="u", web_password="p", sqlite_path=...), now=lambda: NOW)`; `TestClient`)

```python
def test_requires_auth(client): assert client.get("/").status_code == 401
def test_wrong_password_401(client)
def test_page_renders(client_auth):
    r = client_auth.get("/"); assert r.status_code == 200
    assert "esx01.lab.local" in r.text and 'data-snapshot="' in r.text
def test_all_groups_render(client_auth):  # each key in GROUPS → 200 and aria-pressed on that button
def test_unknown_group_falls_back_to_host(client_auth)  # ?group=evil → 200, host grouping
def test_host_column_only_when_not_grouped_by_host(client_auth)  # "<th>Host</th>" absent for host, present for folder
def test_html_escaping(tmp_path):
    # inventory with vm name '<script>x</script>' and description 'a "b"\nc' → raw '<script>x' not in page, '&lt;script&gt;' is
def test_no_db_renders_empty_state(tmp_path)   # sqlite_path to missing file → 200, "No sync yet"
def test_old_schema_renders_empty_state(tmp_path)  # user_version=0 db → 200, "No sync yet"
def test_status_json(client_auth):  # keys snapshot_id, last_run{status,started_at,finished_at,error}, last_success_at, interval_minutes
def test_healthz_no_auth(client):  # 200 {"ok": true, "last_success_age_s": int}
def test_healthz_stale_503(tmp_path)  # now = NOW + 46 min → 503
def test_gzip(client_auth): assert client_auth.get("/", headers={"Accept-Encoding": "gzip"}).headers["content-encoding"] == "gzip"
def test_page_budget_500_vms(tmp_path):
    # generate_db(..., hosts=10, vms=500); gzip size of body < 100_000; median of 5 renders < 0.05 s
def test_static_sizes(): assert (STATIC / "style.css").stat().st_size < 8192
def test_static_cache_headers(client)  # /static/style.css → Cache-Control contains max-age=31536000
```

- [ ] **Step 2: Run** `uv run pytest tests/test_web.py -v` — FAIL.
- [ ] **Step 3: Implement `app.py`.** Auth dependency with `HTTPBasic` + `secrets.compare_digest` on both fields (compare bytes). `GZipMiddleware(minimum_size=500)`. Static mounted at `/static`; a `static_url(name)` Jinja global appends `?v=<first 8 hex of sha256 of file>`, computed once at startup; static responses get `Cache-Control: public, max-age=31536000, immutable`. Each request: `connect_ro` → read → close; `None` connection → empty Inventory and `badge` state `none`. `/healthz` 503 when no success or age > 3× interval. `main()` refuses to start (log error, exit 2) if settings fail validation.
- [ ] **Step 4: Implement `index.html` + `style.css`** by porting `docs/design/mockup.html`: same markup classes and CSS tokens; light tokens on `:root`, dark on `:root[data-theme="dark"]` only (no `prefers-color-scheme`). Group-by buttons are `<a>` links to `?group=key`, styled like the mockup's buttons. Templates rows get class `tpl`; `body:not(.show-tpl) tr.tpl{display:none}`. Badge/banner from `badge()`. Usage bar class `hi` above 85 %. Jinja autoescape on.
- [ ] **Step 5: Run tests** — PASS.
- [ ] **Step 6: Manual check:** `uv run vmdash-mockdb --db mock.db && WEB_USER=a WEB_PASSWORD=b SQLITE_PATH=mock.db uv run vmdash-web`, open `http://localhost:8080` — page matches the mockup's layout with JS disabled (no search, but all rows, all groupings).
- [ ] **Step 7: Commit** `feat: web app with server-rendered dashboard`

---

### Task 6: Client JS (search, collapse, templates, theme, auto-refresh)

**Files:**
- Create: `src/vmdash/web/static/app.js`
- Modify: `src/vmdash/web/templates/index.html` (add `<script src=... defer>`)
- Test: `tests/test_web.py` (size + wiring), manual browser checklist

**Interfaces:**
- Consumes: DOM contract from Task 5; `GET /status.json` shape from Task 5.

- [ ] **Step 1: Add failing tests:** `test_app_js_size` (< 8192 bytes) and `test_page_loads_app_js` (`/static/app.js?v=` in page).
- [ ] **Step 2: Implement `app.js`** (one IIFE, `"use strict"`, no globals). Behaviour, all as in the mockup:
  - **Search:** on `input`, split value on whitespace, lowercase; a row matches when every term is a substring of `data-q`. Hide non-matching rows and groups with no visible rows; while searching ignore collapse. Recompute each group's `.htot`, the `[data-sum]` spans and `#count` (`"<b>N</b> of M VMs"` when searching, else `"<b>M</b> VMs"`) from `data-*` of visible rows. Templates hidden → count matching `tr.tpl` and show `"N more in templates"` button in `#count` that ticks `#tpl`.
  - **Highlight:** when `CSS.highlights` exists, build `Range`s over text nodes of visible rows for each term and set `CSS.highlights.set("q", new Highlight(...ranges))`; style `::highlight(q)` in CSS with the `--hl` token. Skip silently otherwise.
  - **Keys:** `/` (not typing) and `Ctrl/Cmd+K` focus+select `#q`; `Esc` in `#q` clears and blurs. `#kbd` hidden while focused or non-empty.
  - **Collapse:** click on `.gbtn` toggles `collapsed` class on its tbody; state in `sessionStorage["vmdash.collapsed." + group]` as JSON array of keys. `#fold` toggles all and its label `Collapse all` / `Expand all`.
  - **Templates:** `#tpl` toggles `body.show-tpl`; stored in `localStorage["vmdash.tpl"]` (`"1"`/`"0"`).
  - **Theme:** `#theme` toggles `data-theme` light/dark, stores `localStorage["vmdash.theme"]`, swaps sun/moon icon and `aria-label` (`"Switch to dark mode"` / `"Switch to light mode"`).
  - **Auto-refresh:** every 30 s `fetch("/status.json", {cache: "no-store"})`; if `last_run.status === "running"` set badge to running copy; if `snapshot_id` differs from `body.dataset.snapshot`, save `#q` value to `sessionStorage["vmdash.q"]` and `location.reload()`. On load, restore and apply `vmdash.q` then delete it. Fetch errors are ignored.
  - Every storage access wrapped in `try/catch`.
- [ ] **Step 3: Run** `uv run pytest tests/test_web.py -v` — PASS. `node --check src/vmdash/web/static/app.js` if node is installed — no output.
- [ ] **Step 4: Manual browser checklist** (mock DB, run as in Task 5 Step 6):
  1. Type `prod running` → only matching rows, highlights, count "x of y VMs", group totals change.
  2. Search a template name with templates hidden → "1 more in templates" link works.
  3. Collapse a host, reload → still collapsed; switch group → independent state.
  4. Dark toggle → reload → still dark; new private window → light.
  5. Run `uv run vmdash-mockdb --db mock.db` in another shell → within 30 s the page reloads by itself and the search text survives.
- [ ] **Step 5: Commit** `feat: client-side search, collapse, theme and auto-refresh`

---

### Task 7: Collector transform (raw vCenter data → Inventory)

**Files:**
- Create: `src/vmdash/collector/transform.py`
- Test: `tests/test_transform.py`

**Interfaces:**
- Produces:
  - `RawInventory` (dataclass): `vcenter_host: str`, `datacenters: dict[str, dict]`, `folders: dict[str, dict]`, `compute: dict[str, dict]`, `hosts: dict[str, dict]`, `datastores: dict[str, dict]`, `vms: dict[str, dict]`. Keys are morefs (`"vm-101"`). Inner dicts use the exact vSphere property paths from spec §6 as keys (e.g. `"runtime.powerState"`), with these plain-data conversions done by Task 9:
    - `parent`, `runtime.host`, `vmFolder` → moref string
    - `config.hardware.device` → list of `{"key": int, "label": str, "capacity_bytes": int, "datastore": str (moref), "file_name": str}` for `VirtualDisk` devices only
    - `layoutEx` → `{"files": {file_key: size}, "disks": {disk_key: [file_key, ...]}}`
    - `runtime.bootTime` → tz-aware `datetime` or `None`
    - `compute` entries: `{"name": str, "is_cluster": bool}`
  - `datastore_dir(vm_path_name: str) -> str` — `"[ds1] web01/web01.vmx" → "[ds1] web01/"`; `""` for empty.
  - `folder_path(folder_moref: str | None, folders: dict[str, dict], roots: set[str]) -> str` — names joined by `/` from the top, stopping at a moref in `roots` (datacenter `vmFolder`s).
  - `disk_used_bytes(disk_key: int, layout: dict | None) -> int` — 0 when layout or chain missing.
  - `build_inventory(raw: RawInventory, tags: dict[str, list[Tag]]) -> Inventory` — `datacenter` = sorted datacenter names joined with `", "`; `guest_os` = `guest.guestFullName` or `config.guestFullName` or `""`; disk datastore name resolved from datastore moref; host cluster = compute name when `is_cluster` else `None`.

- [ ] **Step 1: Write failing tests**

```python
def test_datastore_dir(): assert datastore_dir("[ds-ssd01] web01/web01.vmx") == "[ds-ssd01] web01/"
def test_folder_path():
    folders = {"group-v1": {"name": "vm", "parent": "datacenter-1"}, "f2": {"name": "Production", "parent": "group-v1"}, "f3": {"name": "Web", "parent": "f2"}}
    assert folder_path("f3", folders, {"group-v1"}) == "Production/Web"
    assert folder_path("group-v1", folders, {"group-v1"}) == ""
def test_disk_used_includes_snapshot_chain():
    layout = {"files": {1: 100, 2: 50, 3: 7}, "disks": {2000: [1, 2]}}
    assert disk_used_bytes(2000, layout) == 150
    assert disk_used_bytes(2001, layout) == 0
def test_build_inventory_full_vm():  # one host in cluster, one VM with 2 disks + tags → every Vm field asserted
def test_guest_os_fallback()
def test_standalone_host_has_no_cluster()
def test_inaccessible_vm_is_kept():
    # vm dict with only {"name": "x", "runtime.powerState": "poweredOff", "runtime.connectionState": "inaccessible"}
    # → Vm(vcpu=0, memory_mb=0, disks=(), description="", datastore_path="", boot_time=None) present
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: collector transform of vCenter data`

---

### Task 8: REST tagging client

**Files:**
- Create: `src/vmdash/collector/tags.py`
- Test: `tests/test_tags.py`

**Interfaces:**
- Produces: `class TagError(Exception)`; `fetch_vm_tags(host: str, user: str, password: str, verify: bool, transport: httpx.BaseTransport | None = None, timeout: float = 30.0) -> dict[str, list[Tag]]` — key = VM moref.
- Calls, in order: `POST https://{host}/api/session` (basic auth) → JSON string token, then header `vmware-api-session-id: <token>`; `GET /api/cis/tagging/category` → ids; `GET /api/cis/tagging/category/{id}` → `{"name"}`; `GET /api/cis/tagging/tag` → ids; `GET /api/cis/tagging/tag/{id}` → `{"name", "category_id"}`; `POST /api/cis/tagging/tag-association?action=list-attached-objects-on-tags` body `{"tag_ids": [...]}` → `[{"tag_id", "object_ids": [{"id", "type"}]}]`, keep `type == "VirtualMachine"`; finally `DELETE /api/session` (errors ignored). Skip the association call when there are no tags. Each VM's tags sorted by `(category, name)`.
- Any HTTP error, timeout or bad JSON → `TagError` whose message has the status/path but never the password or token.

- [ ] **Step 1: Write failing tests** with `httpx.MockTransport` routing on `(method, path)`: `test_maps_tags_to_vms`, `test_ignores_non_vm_objects`, `test_no_tags_skips_association_call`, `test_auth_failure_raises_tag_error` (401 → `TagError`, `"secret"` not in `str(err)`), `test_session_deleted_at_end`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: vSphere REST tagging client`

---

### Task 9: vCenter fetch with pyVmomi

**Files:**
- Create: `src/vmdash/collector/vcenter.py`
- Test: `tests/test_vcenter.py`

**Interfaces:**
- Consumes: `RawInventory` from Task 7, `CollectorSettings`.
- Produces: `connect(settings: CollectorSettings) -> vim.ServiceInstance`; `disconnect(si) -> None`; `fetch_raw(si, settings: CollectorSettings) -> RawInventory`; helpers `to_disks(devices: list) -> list[dict]` and `to_layout(layout_ex) -> dict | None` (pure, tested with `SimpleNamespace` fakes).

- [ ] **Step 1: Write failing tests** for the pure helpers: `test_to_disks_keeps_only_virtual_disks` (fake `vim.vm.device.VirtualDisk` instances via `vim.vm.device.VirtualDisk()` with backing `vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName="[ds1] a/a.vmdk", datastore=<fake with _moId>)` and one `VirtualCdrom` → only the disk returned with moref string); `test_to_layout_maps_files_and_chains` (layoutEx fake with `file=[{key,size}]`, `disk=[{key, chain=[{fileKey=[1,2]}]}]`); `test_to_layout_none`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** `connect`: `SmartConnect(host, user, pwd, disableSslCertValidation=not verify)`; when not verifying, `urllib3.disable_warnings`. `fetch_raw`: root = datacenter named `vcenter_datacenter` (search `rootFolder.childEntity`; raise `ValueError(f"Datacenter not found: {name}")`) or `rootFolder`; for each type (`Datacenter`, `Folder`, `ComputeResource`, `HostSystem`, `Datastore`, `VirtualMachine`) create a `ContainerView`, run one `RetrievePropertiesEx` (follow `ContinueRetrievePropertiesEx` tokens) with the property paths from spec §6, destroy the view. Convert managed-object values to `_moId`, devices via `to_disks`, `layoutEx` via `to_layout`. `compute` entries: `is_cluster = isinstance(obj, vim.ClusterComputeResource)`.
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Optional integration smoke test** (needs Docker/Podman): `podman run -d --rm -p 8989:8989 docker.io/vmware/vcsim -l 0.0.0.0:8989`, then a `.env` with `VCENTER_HOST=localhost:8989`, `VCENTER_USER=user`, `VCENTER_PASSWORD=pass`, `VCENTER_VERIFY_SSL=false`, and `uv run vmdash-collector --once --db sim.db` after Task 10 — expect exit 0 and VMs `DC0_H0_VM0`… in `sim.db`. (vcsim's tagging may differ; `partial` is acceptable here.) Record the result in the commit message; skip if no container runtime.
- [ ] **Step 6: Commit** `feat: vCenter inventory fetch via PropertyCollector`

---

### Task 10: Sync run and collector daemon

**Files:**
- Create: `src/vmdash/collector/sync.py`, `src/vmdash/collector/main.py`
- Test: `tests/test_sync.py`, `tests/test_collector_main.py`

**Interfaces:**
- Consumes: `connect_rw`, `start_run`, `finish_run`, `fail_stale_runs`, `write_snapshot`, `build_inventory`, `fetch_vm_tags`, `TagError`, `connect`, `disconnect`, `fetch_raw`, `setup_logging`, `load_settings`.
- Produces:
  - `run_sync(settings: CollectorSettings, conn: sqlite3.Connection, fetch: Callable[[CollectorSettings], RawInventory] = fetch_inventory, fetch_tags: Callable[[CollectorSettings], dict[str, list[Tag]]] = fetch_tags_for) -> str` — returns `"success" | "partial" | "failed"`; never raises (except `KeyboardInterrupt`/`SystemExit`). `fetch_inventory` and `fetch_tags_for` are module-level defaults wrapping Task 9 / Task 8 (connect → fetch_raw → disconnect in `finally`).
  - Failure error string: `f"{type(e).__name__}: {e}"` truncated to 500 chars; full traceback logged at DEBUG.
  - `run_forever(settings, conn, stop: threading.Event, sync: Callable = run_sync) -> None` — runs `sync`, then `stop.wait(remaining)` where remaining = interval − elapsed (min 0); returns when `stop` is set.
  - `main(argv: list[str] | None = None) -> int` — args from `add_common_args` + `--once`; `setup_logging`; `connect_rw`; `fail_stale_runs` (log count if > 0); `--once` → exit 0 for success/partial, 1 for failed; else install SIGTERM/SIGINT handlers that set `stop` and call `run_forever`. Invalid settings → log error, return 2.

- [ ] **Step 1: Write failing tests** (`tests/test_sync.py`, fakes return a `RawInventory` built with Task 7's dict format)

```python
def test_success_writes_snapshot(conn, settings):
    assert run_sync(settings, conn, fetch=fake_raw, fetch_tags=lambda s: {"vm-1": [Tag("env", "prod")]}) == "success"
    # vms has vm-1, tags has env/prod, last run success with host_count/vm_count set, meta snapshot_id == run id
def test_tag_failure_is_partial(conn, settings):  # fetch_tags raises TagError → "partial", VMs written, no tags
def test_fetch_failure_keeps_old_snapshot(conn, settings):
    run_sync(settings, conn, fetch=fake_raw, fetch_tags=no_tags)
    assert run_sync(settings, conn, fetch=boom, fetch_tags=no_tags) == "failed"
    # vms unchanged; last run failed with error "ConnectionError: unreachable"; meta snapshot_id still first run
def test_error_never_contains_password(conn, settings):  # boom raises Exception(f"bad {password}") → stored error has "***" instead
```
`tests/test_collector_main.py`: `test_run_forever_stops` (stop set from inside fake sync after 2 calls, interval 1 min → returns, 2 calls); `test_once_exit_codes` (monkeypatch `run_sync` → `"failed"` gives 1, `"partial"` gives 0); `test_invalid_settings_exit_2`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** `sync.py` (replace occurrences of the password in error text with `***`) and `main.py`. Log at INFO: `"sync started"`, `"fetched %d hosts, %d VMs, %d datastores"`, `"tags: %d VMs tagged"` or warning `"tags failed: %s"`, `"sync %s in %.1fs"`.
- [ ] **Step 4: Run** `uv run pytest -v` (whole suite) — PASS.
- [ ] **Step 5: Commit** `feat: collector sync run and daemon`

---

### Task 11: Deployment files, README, final verification

**Files:**
- Create: `deploy/systemd/vmdash-collector.service`, `deploy/systemd/vmdash-web.service`, `deploy/INSTALL.md`, `README.md`

- [ ] **Step 1: Write the units.** Both: `User=vmdash`, `Group=vmdash`, `WorkingDirectory=/opt/vmdash`, `EnvironmentFile=/etc/vmdash/vmdash.env`, `StateDirectory=vmdash`, `Restart=on-failure`, `RestartSec=10`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes`, `ReadWritePaths=/var/lib/vmdash`. ExecStart: `/opt/vmdash/.venv/bin/vmdash-collector` and `/opt/vmdash/.venv/bin/vmdash-web`. Web unit `After=network-online.target vmdash-collector.service`.
- [ ] **Step 2: Write `deploy/INSTALL.md`**: create user (`useradd --system --home /opt/vmdash --shell /usr/sbin/nologin vmdash`), copy repo to `/opt/vmdash`, `uv sync --frozen --no-dev`, `install -m 600 -o vmdash .env.example /etc/vmdash/vmdash.env` + edit (`SQLITE_PATH=/var/lib/vmdash/vmdash.db`), copy units to `/etc/systemd/system/`, `systemctl daemon-reload && systemctl enable --now vmdash-collector vmdash-web`, check with `journalctl -u vmdash-collector -f` and `curl -s localhost:8080/healthz`. Note: create a read-only vCenter role for the service account; use TLS (`WEB_SSL_CERT`/`WEB_SSL_KEY` or a reverse proxy) because basic auth sends the password with every request.
- [ ] **Step 3: Write `README.md`**: what it is, screenshot link to `docs/design/mockup.html`, dev quickstart (`uv sync`, `uv run vmdash-mockdb --db mock.db`, run web against it, `uv run pytest`), config table pointer to `.env.example`.
- [ ] **Step 4: Verify units:** `systemd-analyze verify deploy/systemd/*.service` — expected: no errors other than missing `/opt/vmdash` binaries.
- [ ] **Step 5: Final verification:** `uv run ruff check src tests && uv run pytest -v` — all pass; manual run of Task 6 Step 4 checklist once more.
- [ ] **Step 6: Commit** `docs: deployment units, install guide and README`
