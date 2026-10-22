# Installing vmdash on a Linux VM (systemd)

Two services share one SQLite file:

- `vmdash-collector` logs in to vCenter every `SYNC_INTERVAL_MINUTES` and writes the snapshot.
- `vmdash-web` serves the dashboard from that file (read-only).

Requirements: Linux with systemd, [uv](https://docs.astral.sh/uv/), network access to vCenter on port 443.

## 1. vCenter account

Create a service account (e.g. `svc-vmdash@vsphere.local`) and give it the built-in **Read-only** role
on the vCenter root object, propagated to children. Tags are read through the same account.

## 2. Install

```bash
sudo useradd --system --home /opt/vmdash --shell /usr/sbin/nologin vmdash
sudo mkdir -p /opt/vmdash /etc/vmdash
sudo cp -r . /opt/vmdash            # run from a checkout of this repository
sudo chown -R vmdash:vmdash /opt/vmdash

# Everything (virtualenv, Python if uv downloads one, cache) stays under /opt/vmdash,
# so the services work with ProtectHome=yes.
sudo -u vmdash env UV_CACHE_DIR=/opt/vmdash/.uv-cache UV_PYTHON_INSTALL_DIR=/opt/vmdash/.python \
    uv sync --frozen --no-dev --directory /opt/vmdash
```

## 3. Configure

```bash
sudo install -m 600 -o vmdash -g vmdash /opt/vmdash/.env.example /etc/vmdash/vmdash.env
sudo -e /etc/vmdash/vmdash.env
```

Set at least:

```
VCENTER_HOST=vcsa01.example.local
VCENTER_USER=svc-vmdash@vsphere.local
VCENTER_PASSWORD=...
SQLITE_PATH=/var/lib/vmdash/vmdash.db
WEB_USER=admin
WEB_PASSWORD=...
```

Basic auth sends the password with every request. Use TLS: either set `WEB_SSL_CERT` / `WEB_SSL_KEY`
(files readable by the `vmdash` user) or put the site behind a reverse proxy that terminates TLS.

## 4. Test one sync

```bash
sudo -u vmdash sh -c 'set -a; . /etc/vmdash/vmdash.env; set +a; /opt/vmdash/.venv/bin/vmdash-collector --once'
```

Exit code 0 means the snapshot was written (`partial` = VMs written but tags failed; the log says why).

## 5. Start the services

```bash
sudo cp /opt/vmdash/deploy/systemd/vmdash-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vmdash-collector vmdash-web
```

## 6. Check

```bash
journalctl -u vmdash-collector -f          # "sync success in 2.3s: 12 hosts, 340 VMs"
curl -s http://localhost:8080/healthz      # {"ok":true,"last_success_age_s":42}
```

`/healthz` needs no login and returns HTTP 503 when the last successful sync is older than
3 × `SYNC_INTERVAL_MINUTES`, so it can be used by monitoring.

## Updating

```bash
sudo systemctl stop vmdash-web vmdash-collector
# copy the new code to /opt/vmdash, then:
sudo -u vmdash env UV_CACHE_DIR=/opt/vmdash/.uv-cache UV_PYTHON_INSTALL_DIR=/opt/vmdash/.python \
    uv sync --frozen --no-dev --directory /opt/vmdash
sudo systemctl start vmdash-collector vmdash-web
```

The database holds only the latest snapshot. If a new version changes the schema, the collector recreates
the tables on its next sync; the page shows "No sync yet" until then.
