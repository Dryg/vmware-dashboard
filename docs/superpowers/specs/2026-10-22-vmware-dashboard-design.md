# vmdash: vSphere inventory dashboard — design

Date: 2026-10-22 · Status: approved design, pending spec review
Visual reference: [docs/design/mockup.html](../../design/mockup.html) (approved mockup; open in a browser)

## 1. Goal

A read-only, fast-loading web page that shows every VM in a vSphere 8 datacenter, grouped by ESXi host
(or other groupings), with sync status in the header. One vCenter, fewer than 500 VMs.

Success criteria:
- Page shows all VMs on one page (no pagination), hosts sorted A–Z (natural sort).
- Page response under 100 KB gzipped and under 50 ms server time at 500 VMs.
- Frontend never talks to vCenter; it only reads SQLite.
- Collector failure never wipes data; the page shows the last good snapshot plus a clear error.

## 2. Architecture

Two processes, one SQLite file, one Python package.

```
vCenter 8 ──(pyVmomi SOAP + REST tagging)──► vmdash-collector ──writes──► SQLite (WAL)
                                                                              │ reads (read-only)
                                              browser ◄──HTML── vmdash-web ◄──┘
```

- **vmdash-collector**: long-running daemon. Syncs every `SYNC_INTERVAL_MINUTES`. Only writer.
- **vmdash-web**: FastAPI + Jinja2, server-rendered HTML. Opens SQLite with `mode=ro`.
- **vmdash-mockdb**: CLI that generates a deterministic mock database for development and tests.

Stack: Python 3.12+, uv, FastAPI, uvicorn, Jinja2, pydantic-settings, pyVmomi, httpx. Dev: pytest, ruff.

## 3. Repository layout

```
vmware-dashboard/
  pyproject.toml              # uv project, entry points: vmdash-collector, vmdash-web, vmdash-mockdb
  .env.example
  README.md
  src/vmdash/
    config.py                 # Settings (pydantic-settings), --env-file / --db overrides
    log.py                    # setup_logging(level, file)
    models.py                 # dataclasses: Host, Datastore, Vm, Disk, Tag, Inventory
    db.py                     # schema DDL, connect_rw / connect_ro, write_snapshot, sync_runs helpers
    collector/
      vcenter.py              # pyVmomi session + PropertyCollector fetch → raw property dicts
      transform.py            # raw dicts → models (pure, unit-tested)
      tags.py                 # REST tagging client (httpx)
      sync.py                 # run_sync(settings) → one sync_runs row + snapshot
      main.py                 # daemon loop, signals, --once
    web/
      app.py                  # FastAPI app, basic auth, routes, gzip
      queries.py              # SQLite rows → Inventory + SyncStatus
      views.py                # Inventory → view model; grouping, sorting, totals, badge (pure, unit-tested)
      templates/index.html
      static/style.css
      static/app.js           # search, collapse, templates toggle, theme, auto-refresh
    mock/generate.py          # mock database generator
  deploy/systemd/vmdash-collector.service
  deploy/systemd/vmdash-web.service
  deploy/INSTALL.md
  docs/design/mockup.html
  tests/
```

## 4. Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `VCENTER_HOST` | required | e.g. `vcsa01.lab.local` |
| `VCENTER_USER` | required | read-only vCenter role is enough |
| `VCENTER_PASSWORD` | required | never logged |
| `VCENTER_VERIFY_SSL` | `true` | |
| `VCENTER_DATACENTER` | empty | empty = all datacenters |
| `SQLITE_PATH` | `./vmdash.db` | overridden by `--db` CLI flag on all three commands |
| `SYNC_INTERVAL_MINUTES` | `15` | |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FILE` | empty | empty = stdout only (journald under systemd) |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8080` | |
| `WEB_USER` / `WEB_PASSWORD` | required | HTTP basic auth; web refuses to start without them |
| `WEB_SSL_CERT` / `WEB_SSL_KEY` | empty | optional TLS in uvicorn |

`.env` is read from the working directory; `--env-file PATH` overrides. Collector-only settings are not
required by the web process and vice versa (separate settings classes sharing a base).

## 5. Data model (SQLite)

`PRAGMA journal_mode=WAL`, `PRAGMA user_version=1`. If the collector finds a different `user_version`,
it drops and recreates all tables (the data is a re-creatable snapshot).

```sql
sync_runs (id INTEGER PK, started_at TEXT, finished_at TEXT,
           status TEXT,          -- running | success | partial | failed
           error TEXT, host_count INT, vm_count INT)
hosts      (moref TEXT PK, name TEXT, cluster TEXT, connection_state TEXT, in_maintenance INT)
datastores (moref TEXT PK, name TEXT, capacity_bytes INT, free_bytes INT)
vms        (moref TEXT PK, host_moref TEXT NULL, name TEXT, description TEXT, folder_path TEXT,
            power_state TEXT,      -- poweredOn | poweredOff | suspended
            connection_state TEXT, -- connected | disconnected | orphaned | inaccessible | invalid
            is_template INT, vcpu INT, memory_mb INT, guest_os TEXT, boot_time TEXT NULL,
            datastore_path TEXT)   -- e.g. "[ds-ssd01] web01/"
disks      (vm_moref TEXT, key INT, label TEXT, capacity_bytes INT, used_bytes INT, datastore TEXT)
tags       (vm_moref TEXT, category TEXT, name TEXT)
meta       (key TEXT PK, value TEXT)  -- vcenter_host, datacenter, sync_interval_minutes, snapshot_id, snapshot_uid
```

`meta` is written with each snapshot so the web process needs no vCenter settings: it reads the header
text and the stale threshold from here. `snapshot_id` = id of the sync run that wrote the snapshot;
`snapshot_uid` = random id per written snapshot (run ids restart when the database is recreated).

Timestamps are ISO 8601 UTC; the web renders them in the server's local time as `DD.MM.YYYY HH:MM`.
Only the latest snapshot is kept. `sync_runs` keeps the last 500 rows.

## 6. Collector

**One sync run:**
1. Insert `sync_runs` row with status `running`.
2. Connect with pyVmomi (`SmartConnect`, SSL context per `VCENTER_VERIFY_SSL`).
3. Fetch with one PropertyCollector call per object type over a ContainerView:
   - Datacenter: `name`, `vmFolder`
   - Folder: `name`, `parent` (to build folder paths; the datacenter's root `vm` folder is excluded)
   - HostSystem: `name`, `parent` (cluster or standalone compute resource name), `runtime.connectionState`, `runtime.inMaintenanceMode`
   - Datastore: `name`, `summary.capacity`, `summary.freeSpace`
   - VirtualMachine: `name`, `parent`, `config.annotation`, `config.template`, `config.hardware.numCPU`,
     `config.hardware.memoryMB`, `config.hardware.device`, `config.files.vmPathName`, `config.guestFullName`,
     `guest.guestFullName`, `runtime.powerState`, `runtime.connectionState`, `runtime.host`, `runtime.bootTime`, `layoutEx`
4. Transform (pure functions in `transform.py`):
   - `datastore_path` = `vmPathName` with the `.vmx` filename stripped.
   - `guest_os` = `guest.guestFullName` if set, else `config.guestFullName`.
   - Disks = `VirtualDisk` devices: label, `capacityInBytes`, datastore name from backing.
   - Used bytes per disk = sum of `layoutEx.file[].size` for every `fileKey` in that disk's
     `layoutEx.disk[key].chain` (includes snapshot deltas).
   - VMs whose VM properties are missing (inaccessible) are kept with what is available.
5. Fetch tags via REST (`tags.py`): `POST /api/session` → list categories and tags →
   `POST /api/cis/tagging/tag-association?action=list-attached-objects-on-tags`. Map by VM moref.
   On any tagging error: log a warning, continue with no tags, final status `partial`.
6. `write_snapshot()`: one transaction: delete from `hosts, datastores, vms, disks, tags`; insert all.
7. Update the run row: `success` / `partial`, counts, `finished_at`. Disconnect.

**Failure:** any exception before step 6 → run row `failed` with a one-line error, old snapshot untouched.

**Daemon (`main.py`):** on start, create/upgrade schema and mark leftover `running` rows `failed`
("collector stopped during sync"). Then loop: run sync, sleep until next interval (wall clock aligned to
start time), exit cleanly on SIGTERM/SIGINT (finishes or abandons the current run, marking it failed).
`--once` runs a single sync and exits with code 0 on success/partial, 1 on failure.

## 7. Web

**Routes**
- `GET /` (auth): full page. Query `?group=host|folder|cluster|datastore|power|uptime|os` (default `host`).
- `GET /status.json` (auth): `{"snapshot_id", "snapshot_uid", "last_run": {"status","started_at","finished_at","error"}, "last_success_at", "interval_minutes"}`.
- `GET /healthz` (no auth): `200 {"ok": true, "last_success_age_s": N}` or `503` if no success in 3× interval.
- `GET /static/*`: long cache headers; URL carries a content hash query string for cache busting.
- GZip middleware on all responses.

**Auth:** FastAPI `HTTPBasic`, `secrets.compare_digest` on user and password.

**Page (matches mockup):**
- Header row 1: `vmdash`, vCenter host · datacenter, search box (`/` or `Ctrl+K` focuses, `Esc` clears),
  sync badge, theme toggle (top right).
- Header row 2: Group by buttons (links), Show templates checkbox, Collapse all / Expand all, VM count.
- Summary line: hosts, VMs (running), vCPU, RAM GB, provisioned / used.
- Table columns: VM (name + description; folder · guest OS · tags) | Host (only when not grouped by host)
  | Status (+ uptime) | vCPU | RAM GB | Location | Disks (per disk: label, provisioned, usage bar, used)
  | Total (provisioned, bar, used). Usage bar turns amber above 85 %.
- Group header: name, extras, totals (VMs, vCPU, RAM, provisioned).

**Display status** (derived in `views.py`): template → Template; VM connection `orphaned` → Orphaned;
`disconnected`/`inaccessible`/`invalid` → Unknown; else Running / Shut down / Suspended.

**Groupings and order** (VMs inside a group always A–Z natural sort):

| Group | Key | Group order | Header extras |
|---|---|---|---|
| Host (default) | host name, or "No host" | natural A–Z, "No host" last | cluster, connection state |
| Folder | first segment of folder path | A–Z | — |
| Cluster | host's cluster, or "No cluster" | A–Z, "No cluster" last | — |
| Datastore | datastore of the VM folder | A–Z | usage bar, used of capacity (%) |
| Power state | display status | Running, Suspended, Shut down, Unknown, Orphaned, Template | — |
| Uptime | bucket from `boot_time` | <1 day, 1–7 d, 7–30 d, 30–90 d, >90 d, Not running | — |
| Guest OS | `guest_os` | A–Z | — |

**Client JS (`app.js`, under 8 KB unminified, no libraries, no build step):**
- Search: each VM row carries `data-q` (lowercased text of every visible field plus host, cluster).
  Space-separated terms, all must match. Non-matching rows hidden; empty groups hidden; group totals,
  summary and count recomputed from `data-*` numbers on rows. Matches highlighted with the CSS Custom
  Highlight API (no highlight on browsers without it). Collapsed groups expand while searching.
  When templates are hidden and some match: "N more in templates" link.
- Templates toggle: templates are rendered with class `tpl` and hidden by CSS; choice in `localStorage`.
- Collapse: per group, stored in `sessionStorage`.
- Theme: light by default; toggle sets `data-theme="dark"` on `<html>`, stored in `localStorage`
  (`vmdash.theme`). A tiny inline script in `<head>` applies it before first paint (no flash).
- Auto-refresh: poll `/status.json` every 30 s. Badge updates in place (Syncing…). When `snapshot_uid`
  changes, reload; search text is kept in `sessionStorage` and restored.

**Sync badge states:** green "Synced <time>" (+ next sync time); amber pulsing "Syncing… started <time>";
red "Sync failed <time>, showing data from <last success>" + error banner; amber "Data is N min old" when
the last success is older than 3× interval; grey "No sync yet" empty state when the DB has no snapshot.

**Missing/invalid DB:** page renders with the "No sync yet" empty state, never a 500.

**Styling:** system font stack, no web fonts, one CSS file (under 8 KB unminified), colors as CSS tokens
with a light set on `:root` and a dark set on `:root[data-theme="dark"]`.

## 8. Logging

`log.py` configures the root logger: `%(asctime)s %(levelname)s %(name)s: %(message)s`, stdout handler,
plus `RotatingFileHandler` (10 MB × 5) when `LOG_FILE` is set. uvicorn loggers use the same handlers.
Collector logs: run start, per-type object counts, tag result, duration, errors with traceback at DEBUG.
Passwords and session tokens are never logged.

## 9. Mock database

`vmdash-mockdb --db mock.db [--hosts 6] [--vms 80] [--seed 42]`: deterministic output for a given seed.
Covers: several clusters, one host in maintenance mode, one disconnected host, one orphaned VM with no
host, templates, all power states, 1–4 disks per VM with a datastore above 85 % full, multi-category tags,
long descriptions, mixed guest OS (including an old Windows Server 2012 R2), uptime across all buckets,
and a sync history with one failed run. Uses the same `db.write_snapshot()` as the collector.

## 10. Testing

pytest, no live vCenter needed.
- `transform.py`: disk used-size from fake `layoutEx`, datastore path parsing, folder paths, guest OS fallback.
- `tags.py`: httpx `MockTransport` for success and failure.
- `sync.py`: fake fetcher → snapshot written; fetch failure → `failed` run, old data kept; tag failure → `partial`.
- `views.py`: every grouping's keys and order, natural sort, display status, totals, uptime buckets.
- `app.py` (TestClient on mock DB): 401 without auth, 200 with, each `?group=`, `/status.json`,
  `/healthz` 200/503, empty DB state, gzip, page size budget at 500 VMs.
- `mock/generate.py`: same seed → identical DB.

## 11. Deployment (systemd)

- User `vmdash`, code in `/opt/vmdash` (uv venv), config `/etc/vmdash/vmdash.env` (mode 600),
  data in `/var/lib/vmdash` via `StateDirectory=vmdash`.
- Two units with `EnvironmentFile=`, `Restart=on-failure`, `ProtectSystem=strict`, `NoNewPrivileges=yes`,
  `PrivateTmp=yes`. `deploy/INSTALL.md` lists the commands.

## 12. Out of scope (possible later)

Snapshot age and VMware Tools status columns/groupings; multiple vCenters; history/trends;
"Sync now" button; per-user auth.
