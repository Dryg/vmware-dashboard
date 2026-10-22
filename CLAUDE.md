# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                              # install (Python >= 3.12, deps pinned in uv.lock)
uv run pytest                                        # all tests; no vCenter needed
uv run pytest tests/test_views.py::test_badge_ok     # one test
uv run ruff check src tests                          # lint (line length 120)

uv run vmdash-mockdb --db mock.db                    # deterministic mock DB: 6 hosts, 80 VMs
WEB_USER=admin WEB_PASSWORD=admin SQLITE_PATH=mock.db uv run vmdash-web   # http://localhost:8080
```

End-to-end against the vCenter simulator (no tagging API, so the sync ends `partial`):

```bash
podman run -d --rm -p 8989:8989 docker.io/vmware/vcsim -l 0.0.0.0:8989
VCENTER_HOST=localhost:8989 VCENTER_USER=user VCENTER_PASSWORD=pass VCENTER_VERIFY_SSL=false \
  uv run vmdash-collector --once --env-file /dev/null --db sim.db
```

`WEB_PASSWORD=change-me` is rejected on purpose (the placeholder in the example env files).

## Architecture

Two processes share one SQLite file and never talk to each other:

- **Collector** (`src/vmdash/collector/`) is the only writer. `vcenter.py` reads vCenter with one
  PropertyCollector call per object type and converts every value to plain dicts/strings (`CONVERT` maps a
  property path to its converter). `transform.py` turns that `RawInventory` into the `models.Inventory`
  dataclasses with pure functions. `sync.run_sync` then merges data kept from the previous snapshot
  (`keep_last_network`: last-known IPs/DNS with `net_seen_at`; previous tags when the tag fetch fails) and
  replaces the whole snapshot in one transaction (`db.write_snapshot`). `run_sync` must never raise: the
  daemon loop relies on it.
- **Tags** come from the vSphere REST API (`tags.py`, httpx), separately from pyVmomi. A tag failure makes the
  run `partial`, not `failed`; VM data is still written.
- **Web** (`src/vmdash/web/`) opens the DB read-only (`db.connect_ro`), caches the parsed inventory keyed by
  `meta.snapshot_uid`, and on a read error serves the last good status and inventory. `views.py` is pure logic
  (grouping, status, search text, sync badge). `fragments.py` builds each VM row as an HTML string in Python
  (Jinja was too slow for 500 rows), so every inventory value there must go through `escape`.
- **Browser** (`static/app.js`, no libraries) filters rows client-side from two attributes rendered per row:
  `data-q` (the lowercase search text from `views._row`) and `data-n` (numbers for live totals, format in
  `fragments.row_numbers`). It polls `status.json` every 30 s and reloads when `snapshot_uid` changes.

### Schema changes

`db.SCHEMA_VERSION` must be bumped whenever a table changes. `connect_rw` drops and recreates all tables on a
version mismatch; `connect_ro` returns None, so the page shows "No sync yet" until the collector runs once.
The DB is otherwise a disposable cache, except that last-known network data and fallback tags are read back
from the previous snapshot.

Adding a VM field usually touches, in order: `models.Vm` → `vcenter.VM_PROPS`/`CONVERT` → `transform.build_inventory`
→ `db` (schema, `write_snapshot`, `read_inventory`) → `views._row` (search text) → `fragments.row_html` →
`mock/generate.py` → tests.

## Constraints enforced by tests

- Page render for 500 VMs: median under 50 ms per grouping, gzipped page under 100 KB
  (`test_page_budget_500_vms`; can fail on a busy machine, rerun before assuming a regression).
- `static/style.css` and `static/app.js` stay under 8192 bytes each.
- The CSP allows exactly one inline script, `app.BOOT_JS`, by its hash. Do not add inline scripts or event
  handler attributes; put code in `app.js`.
- Static and status URLs are relative (`static/...`, `status.json`), so the site works behind a reverse proxy
  under a path prefix (`examples/nginx/vmdash-subpath.conf`). Do not add leading slashes.
- The mock generator is deterministic and tests assert the cases it covers (orphaned VM, templates, IP conflict,
  last-seen network, ...). New `rng` calls shift all later random values; derive new mock fields from the index.

Tests import helpers from `tests/factories.py` (`make_vm`, `make_inventory`, fixed `NOW`); `pythonpath` includes
`tests/`.

## Deployment

`deploy/INSTALL.md` is the install guide; `examples/` holds the env file, systemd units and nginx configs it
copies. The units are heavily sandboxed (`MemoryDenyWriteExecute`, syscall filter, `ProtectSystem=strict`):
a new dependency with native code or JIT should be tried under those settings before release.
