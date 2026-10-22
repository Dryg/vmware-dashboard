# vmdash

A fast, read-only dashboard for a vSphere datacenter. Every VM on one page, grouped by ESXi host
(or folder, cluster, datastore, power state, uptime, guest OS), with instant search across all fields.

- **vmdash-collector** reads vCenter (pyVmomi + REST tagging API) every few minutes and writes a snapshot to SQLite.
- **vmdash-web** renders the page from SQLite only (FastAPI + Jinja2, ~8 KB of CSS, ~8 KB of plain JS).
  It never talks to vCenter.

The design reference is [docs/design/mockup.html](docs/design/mockup.html) (open it in a browser).
Spec: [docs/superpowers/specs/2026-10-22-vmware-dashboard-design.md](docs/superpowers/specs/2026-10-22-vmware-dashboard-design.md).

## Try it with mock data

```bash
uv sync
uv run vmdash-mockdb --db mock.db                     # 6 hosts, 80 VMs, deterministic
WEB_USER=admin WEB_PASSWORD=admin SQLITE_PATH=mock.db uv run vmdash-web
# open http://localhost:8080  (admin / admin)
```

## Run against vCenter

```bash
cp .env.example .env          # fill in VCENTER_* and WEB_*
uv run vmdash-collector --once   # one sync, exit code 1 on failure
uv run vmdash-collector          # keeps syncing every SYNC_INTERVAL_MINUTES
uv run vmdash-web
```

All commands accept `--env-file PATH` and `--db PATH` (overrides `SQLITE_PATH`).
Every setting is documented in [.env.example](.env.example).
Production install with systemd: [deploy/INSTALL.md](deploy/INSTALL.md).

## Page features

- Search box (`/` or `Ctrl+K`): every word must match; searches name, description, folder, tags, OS,
  status, uptime, host, cluster, datastore, sizes, DNS name, network, MAC and IP addresses.
- Network column: the guest's DNS name and every network adapter with its port group and IPs
  (IPs and DNS name come from VMware Tools, so they are empty for powered-off VMs or VMs without Tools).
- Group by host (default), folder, cluster, datastore, power state, uptime or guest OS (`?group=` in the URL).
- Show templates toggle, collapsible groups, light/dark theme (remembered per browser).
- The header shows sync status; the page reloads itself when a new snapshot arrives.
- `GET /healthz` (no auth) for monitoring; `GET /status.json` for the sync state.

## Development

```bash
uv run pytest          # no vCenter needed
uv run ruff check src tests
```

Optional end-to-end check against the vCenter simulator:

```bash
podman run -d --rm -p 8989:8989 docker.io/vmware/vcsim -l 0.0.0.0:8989
printf 'VCENTER_HOST=localhost:8989\nVCENTER_USER=user\nVCENTER_PASSWORD=pass\nVCENTER_VERIFY_SSL=false\n' > sim.env
uv run vmdash-collector --once --env-file sim.env --db sim.db   # "partial": vcsim has no tagging API
```
