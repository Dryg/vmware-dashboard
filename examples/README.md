# Example configs

Production layout from [deploy/INSTALL.md](../deploy/INSTALL.md): code in `/opt/vmdash`, settings in
`/etc/vmdash/vmdash.env`, database in `/var/lib/vmdash`, nginx in front for TLS.

| File | Copy to | What it is |
|---|---|---|
| [vmdash.env](vmdash.env) | `/etc/vmdash/vmdash.env` (mode 600, owner vmdash) | Settings for both services |
| [systemd/vmdash-collector.service](systemd/vmdash-collector.service) | `/etc/systemd/system/` | Backend: syncs vCenter into SQLite |
| [systemd/vmdash-web.service](systemd/vmdash-web.service) | `/etc/systemd/system/` | Frontend: serves the dashboard on 127.0.0.1:8080 |
| [nginx/vmdash-root.conf](nginx/vmdash-root.conf) | `/etc/nginx/conf.d/` | Default endpoint: `https://vmdash.example.com/` |
| [nginx/vmdash-subpath.conf](nginx/vmdash-subpath.conf) | `/etc/nginx/conf.d/` | Custom endpoint: `https://intranet.example.com/vmdash/` |

Use one of the two nginx files. Both forward to the app, which asks for its own login
(`WEB_USER` / `WEB_PASSWORD`); nginx adds TLS and redirects HTTP to HTTPS.
