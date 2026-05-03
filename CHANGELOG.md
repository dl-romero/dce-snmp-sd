# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [1.1.0] — 2026-05-03

### Added

#### Data Center Expert integration
- `app/dce_client.py` — SOAP client for the Schneider Electric Data Center Expert web services API (`/integration/services/ISXCentralDeviceService_v2_0`); uses HTTP Basic Auth (credentials sent per-request, no sessions); stdlib `urllib.request` only (no extra dependencies); runs blocking SOAP calls in a thread pool to stay async-safe
- `app/dce_store.py` — JSON-backed async-safe store for DCE server configurations (same atomic write + lock pattern as `DeviceStore`)
- `app/dce_sync.py` — hourly scheduler that syncs all enabled DCE servers concurrently:
  - New devices in DCE → added to inventory, SNMP probe triggered for module resolution
  - Devices removed from DCE → removed from inventory (DCE-sourced entries only; manual entries are never touched)
  - Existing devices → hostname and location labels updated from DCE if changed
- DCE CRUD routes under `/api/dce`:
  - `GET /api/dce` — list all servers (passwords excluded from responses)
  - `POST /api/dce` — add server; triggers an initial sync immediately
  - `GET /api/dce/{id}` — get a server
  - `PATCH /api/dce/{id}` — update host, credentials, community, auth, or enabled flag
  - `DELETE /api/dce/{id}` — remove server and all devices imported from it
  - `POST /api/dce/{id}/sync` — force immediate sync from one server
  - `POST /api/dce/sync` — force immediate sync from all enabled servers
- `source` field on `DeviceRecord` and `DeviceOut`: `"manual"` for user-added devices, `"dce:<server-id>"` for DCE-imported devices
- `DCE_SYNC_INTERVAL_HOURS` environment variable (default: `1`)
- `DCE_SERVERS_FILE` environment variable (default: same directory as `DATA_FILE`)
- DCE server status (`total`, `enabled`) added to `GET /health` response
- Full DCE integration section in `README.md`

---

## [1.0.0] — 2026-05-03

### Added

#### Core service (`app/`)
- FastAPI HTTP service discovery server for [prometheus/snmp_exporter](https://github.com/prometheus/snmp_exporter)
- Persistent device inventory backed by a JSON file (`devices.json`), with atomic writes and an asyncio lock for concurrency safety
- REST API for adding, updating, and removing SNMP devices
- Automatic module discovery on device add: probes `sysObjectID`, `sysName`, and `sysDescr` in a single SNMP GET, then resolves the correct snmp_exporter module via three-tier lookup:
  1. Exact `sysObjectID` match against `module_lookup.json`
  2. Longest-prefix `sysObjectID` match (wildcard device families)
  3. Vendor MIB OID-existence probe (fallback for devices without sysObjectID rules)
- Background hostname refresh every 6 hours (configurable) using `asyncio.Semaphore(20)` for concurrent probes
- `GET /targets` — Prometheus http_sd endpoint; excludes devices with no resolved module until discovery completes
- `GET /health` — service health with device inventory stats and running config
- `POST /api/reload-lookup` — reloads `module_lookup.json` from disk without restarting; intended for use after the daily DDF sync
- `POST /api/devices/{ip}/refresh` and `POST /api/refresh` — on-demand re-probe for one or all devices
- PATCH support for community string (triggers re-probe), auth, module override (set to `""` to revert to auto-discovery), and labels
- `POST /api/devices/batch` — add multiple devices in a single request
- Reverse DNS fallback for hostname when `sysName` is unavailable
- `module_override` field takes precedence over auto-discovered module; `effective_module` reflects whichever is active
- `ready` flag on each device record — `true` once an effective module is set

#### Deployment
- `install.sh` — one-command Linux installer: creates `dce-snmp-sd` system user, installs app to `/opt/dce-snmp-sd`, creates Python venv, patches and enables systemd unit
- `systemd/dce-snmp-sd.service` — simple systemd unit with `Restart=on-failure`, journal logging, and all env vars configurable via `systemctl edit`
- `Dockerfile` — layered build (dependencies as a separate layer), `/data` volume mount point, `HEALTHCHECK` via urllib, all five env vars set as `ENV` defaults
- `docker-compose.yml` — full stack with `dce-snmp-sd` and `prom/snmp-exporter`, all parameters driven by `.env`
- `.env.example` — documents all configurable Docker parameters with defaults

#### Documentation
- Full `README.md`: how it works, installation (Linux systemd and Docker), configuration, complete REST API reference with request/response examples, Prometheus relabel config, Docker quick-start, troubleshooting guide

[Unreleased]: https://github.com/dl-romero/dce-snmp-sd/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/dl-romero/dce-snmp-sd/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/dl-romero/dce-snmp-sd/releases/tag/v1.0.0
