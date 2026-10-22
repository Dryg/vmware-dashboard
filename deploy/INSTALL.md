# Installing vmdash on a Linux VM (systemd)

Two services share one SQLite file:

- `vmdash-collector` logs in to vCenter every `SYNC_INTERVAL_MINUTES` and writes the snapshot.
- `vmdash-web` serves the dashboard from that file (read-only).

Requirements: Linux with systemd, [uv](https://docs.astral.sh/uv/), network access to vCenter on port 443.

## 1. vCenter account

Create a service account (e.g. `svc-vmdash@vsphere.local`) with the built-in **Read-only** role as a
**global permission**, propagated to children. Step by step, including the password-expiry trap:
[README: Read-only vCenter account](../README.md#read-only-vcenter-account).

## 2. Install

```bash
sudo useradd --system --home /opt/vmdash --shell /usr/sbin/nologin vmdash
sudo mkdir -p /opt/vmdash /etc/vmdash
sudo install -d -o vmdash -g vmdash -m 750 /var/lib/vmdash

# From a checkout of this repository. `git archive` copies only tracked files,
# so no local .env, .venv or *.db ends up on the server.
git archive HEAD | sudo tar -x -C /opt/vmdash
sudo chown -R vmdash:vmdash /opt/vmdash

# Everything (virtualenv, Python if uv downloads one, cache) stays under /opt/vmdash,
# so the services work with ProtectHome=yes. The full path to uv is used because
# sudo's secure_path often does not include ~/.local/bin.
UV=$(command -v uv)
sudo -u vmdash env UV_CACHE_DIR=/opt/vmdash/.uv-cache UV_PYTHON_INSTALL_DIR=/opt/vmdash/.python \
    "$UV" sync --frozen --no-dev --directory /opt/vmdash
```

## 3. Configure

```bash
sudo install -m 600 -o vmdash -g vmdash /opt/vmdash/examples/vmdash.env /etc/vmdash/vmdash.env
sudo -e /etc/vmdash/vmdash.env
```

Every setting is described in `/opt/vmdash/.env.example`.

Do not leave a `.env` file in `/opt/vmdash`: the services run there and would read it for any
setting missing from `/etc/vmdash/vmdash.env`.

Set at least `VCENTER_HOST`, `VCENTER_USER`, `VCENTER_PASSWORD` and `WEB_PASSWORD`.

The example listens on `127.0.0.1:8080` for nginx (step 7). Basic auth sends the password with every
request, so without a reverse proxy set `WEB_HOST=0.0.0.0` and `WEB_SSL_CERT` / `WEB_SSL_KEY`
(files readable by the `vmdash` user) instead.

## 4. Test one sync

```bash
sudo -u vmdash /opt/vmdash/.venv/bin/vmdash-collector --once --env-file /etc/vmdash/vmdash.env
```

Exit code 0 means the snapshot was written (`partial` = VMs written but tags failed; the log says why).

## 5. Start the services

```bash
sudo cp /opt/vmdash/examples/systemd/vmdash-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vmdash-collector vmdash-web
```

## 6. Check

```bash
journalctl -u vmdash-collector -f          # "sync success in 2.3s: 12 hosts, 340 VMs"
curl -s http://localhost:8080/healthz      # {"ok":true,"last_success_age_s":42}
```

`/healthz` needs no login and returns HTTP 503 when the last successful sync is older than
3 × `SYNC_INTERVAL_MINUTES`. It also returns `"last_run"`: alert on `"failed"` to hear about the first failed
sync, warn on `"partial"` (VMs synced, tags could not be read).

## Security notes

- Both units run sandboxed (no capabilities, read-only system, private /tmp and devices, syscall filter);
  `systemd-analyze security vmdash-web` should report about 1.1 OK. The database is readable only by `vmdash`.
- The web app sends a strict Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff`, `no-referrer` and
  `Cache-Control: no-store` on pages, and refuses to start while `WEB_PASSWORD` is still `change-me`.
- Failed logins show as `401` in `journalctl -u vmdash-web` and in the nginx access log (for fail2ban);
  the nginx examples also rate-limit each client to 5 requests/s.
- `VCENTER_VERIFY_SSL=false` logs a warning at every start. Keep verification on and point
  `VCENTER_CA_FILE` at the vCenter CA instead: download `https://<vcenter>/certs/download.zip`, take the
  `.crt` file(s) from `certs/lin/`, `cat` them into `/etc/vmdash/vcenter-ca.pem` (readable by `vmdash`).

## 7. nginx reverse proxy (TLS)

Pick one example, change `server_name` and the certificate paths:

- `examples/nginx/vmdash-root.conf`: the dashboard is the whole site, `https://vmdash.example.com/`.
- `examples/nginx/vmdash-subpath.conf`: the dashboard under a custom path on a shared host,
  `https://intranet.example.com/vmdash/`. To add it to an existing server, copy its two `location` blocks.

```bash
sudo cp /opt/vmdash/examples/nginx/vmdash-root.conf /etc/nginx/conf.d/vmdash.conf
sudo -e /etc/nginx/conf.d/vmdash.conf
sudo nginx -t && sudo systemctl reload nginx
```

On SELinux systems (RHEL, Rocky, Fedora) nginx may not connect to the app until you run
`sudo setsebool -P httpd_can_network_connect 1`.

## Updating

```bash
sudo systemctl stop vmdash-web vmdash-collector
git archive HEAD | sudo tar -x -C /opt/vmdash      # from the updated checkout
sudo chown -R vmdash:vmdash /opt/vmdash
sudo -u vmdash env UV_CACHE_DIR=/opt/vmdash/.uv-cache UV_PYTHON_INSTALL_DIR=/opt/vmdash/.python \
    "$(command -v uv)" sync --frozen --no-dev --directory /opt/vmdash
sudo systemctl start vmdash-collector vmdash-web
```

The database holds only the latest snapshot. If a new version changes the schema, the collector recreates
the tables on its next sync; the page shows "No sync yet" until then.
