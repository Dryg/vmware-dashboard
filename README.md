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

## Read-only vCenter account

The collector only reads, so give it its own account with the built-in **Read-only** role and nothing else.
Steps for the vSphere Client 7 and 8 (you need an administrator login):

1. **Create the user.** Menu → **Administration** → **Single Sign On** → **Users and Groups** → **Users** tab.
   Pick the domain `vsphere.local`, click **Add**, set the username `svc-vmdash` and a long random password.
   (An Active Directory service account also works; skip this step and use `svc-vmdash@your.domain`.)
2. **Give it Read-only as a global permission.** Menu → **Administration** → **Access Control** →
   **Global Permissions** → **Add**. Domain `vsphere.local`, user `svc-vmdash`, role **Read-only**,
   tick **Propagate to children**, click **OK**.
3. **Check it.** Log in to the vSphere Client as `svc-vmdash@vsphere.local`: you should see every VM and tag,
   and every action (power, edit, delete) should be greyed out. Then run `vmdash-collector --once`.

**Why a global permission:** tags and tag categories are global objects. A permission set only on the vCenter
object gives the collector all VM data but no tags, and the page then shows "tags failed".

**Privileges:** the Read-only role contains exactly `System.Anonymous`, `System.View` and `System.Read`.
That covers everything vmdash reads: inventory, hardware, disks and file sizes, guest info from VMware Tools
(IPs, DNS name), hosts, clusters, datastores and tags. To limit vmdash to one datacenter, set
`VCENTER_DATACENTER`; the permission can stay global.

**Avoid two outages:**

- **Password expiry.** `vsphere.local` passwords expire after 90 days by default, and the sync stops working
  then. Either raise *Maximum lifetime* under Administration → Single Sign On → Configuration →
  Local Accounts → Password Policy (it applies to every `vsphere.local` user), or put a reminder in your calendar.
  AD accounts follow your domain's policy.
- **Lockout.** After 5 wrong passwords (default) the account is locked. A wrong `VCENTER_PASSWORD` fails every
  sync, so check `journalctl -u vmdash-collector` after changing it.

## Run against vCenter

```bash
cp .env.example .env          # fill in VCENTER_* and WEB_*
uv run vmdash-collector --once   # one sync, exit code 1 on failure
uv run vmdash-collector          # keeps syncing every SYNC_INTERVAL_MINUTES
uv run vmdash-web
```

All commands accept `--env-file PATH` and `--db PATH` (overrides `SQLITE_PATH`).
Every setting is documented in [.env.example](.env.example).
Production install with systemd and nginx: [deploy/INSTALL.md](deploy/INSTALL.md);
the config files it uses are in [examples/](examples/).

## Page features

- Search box (`/` or `Ctrl+K`): every word must match; searches name, description, folder, tags, OS,
  status, uptime, host, cluster, datastore, sizes, DNS name, network, MAC and IP addresses.
- Network column: the guest's DNS name and every network adapter with its port group and IPs.
  IPs and DNS name come from VMware Tools; when a VM is off (or Tools stops reporting) the last known
  values stay, greyed out with "last seen <date>". An IP found on more than one VM is shown in red with
  the other VM's name (search `ip conflict` to list them all).
- Group by host (default), folder, cluster, datastore, power state, uptime or guest OS (`?group=` in the URL).
- Show templates toggle, collapsible groups, light/dark theme (remembered per browser).
- The header shows sync status; the page reloads itself when a new snapshot arrives. A failed sync turns it
  red with the error in a banner; a sync where only tags failed turns it orange (tags from the previous sync stay).
- `GET /healthz` (no auth) for monitoring: HTTP 503 when the data is older than 3 sync intervals, and
  `"last_run": "success" | "partial" | "failed"` to alert on the first failure. `GET /status.json` for the sync state.

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
