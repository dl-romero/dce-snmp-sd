# snmp-http-sd

Prometheus [HTTP service discovery](https://prometheus.io/docs/prometheus/latest/http_sd/) server for [snmp_exporter](https://github.com/prometheus/snmp_exporter).

Manages an inventory of SNMP device IPs via a REST API. When a device is added, the service automatically probes it over SNMP to resolve its hostname and the correct snmp_exporter module (using the `module_lookup.json` from [ddf-to-snmp-exporter](https://github.com/dl-romero/ddf-to-snmp-exporter)). Hostnames are re-checked every 6 hours so the `/targets` endpoint stays current as devices change.

---

## How it works

```
┌──────────────┐   REST API   ┌─────────────────┐   http_sd   ┌────────────┐
│   You / ops  │ ──────────── │  snmp-http-sd   │ ─────────── │ Prometheus │
└──────────────┘  add/remove  └────────┬────────┘             └─────┬──────┘
                                       │ SNMP probe                 │ scrape
                              ┌────────▼────────┐             ┌─────▼──────┐
                              │  Your devices   │ ◄────────── │snmp_export │
                              └─────────────────┘             └────────────┘
```

1. You POST a device IP to `/api/devices`
2. The service immediately probes it via SNMP — fetching `sysName`, `sysObjectID`, and `sysDescr`
3. `sysObjectID` is matched against the DDF lookup index to find the right snmp_exporter module
4. The device appears in `/targets` with `__param_module` and `__param_auth` labels set
5. Prometheus polls `/targets` and routes scrapes through snmp_exporter
6. Every 6 hours the service re-probes all devices and updates hostnames if they change

---

## Requirements

- Python 3.10+
- [`ddf-to-snmp-exporter`](https://github.com/dl-romero/ddf-to-snmp-exporter) — provides `output/module_lookup.json`

---

## Repository structure

```
snmp-http-sd/
├── app/
│   ├── main.py            # FastAPI app, routes, lifespan
│   ├── models.py          # Pydantic models
│   ├── store.py           # JSON-backed async device store
│   ├── snmp_discovery.py  # SNMP probing + module resolution
│   └── scheduler.py       # Background refresh loop
├── systemd/
│   └── snmp-http-sd.service
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── install.sh
└── requirements.txt
```

---

## Installation

```bash
git clone https://github.com/dl-romero/snmp-http-sd.git
cd snmp-http-sd
sudo bash install.sh
```

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--lookup PATH` | `/opt/ddf-to-snmp-exporter/output/module_lookup.json` | Path to DDF module lookup index |
| `--port PORT` | `8000` | Port to listen on |
| `--host HOST` | `0.0.0.0` | Bind address |

**Custom paths example:**
```bash
sudo bash install.sh \
  --lookup /data/ddf-to-snmp-exporter/output/module_lookup.json \
  --port 9200
```

### What gets installed

```
/opt/snmp-http-sd/          app files + venv
/var/lib/snmp-http-sd/      devices.json persistent store
/etc/systemd/system/snmp-http-sd.service
```

A `snmp-http-sd` system user is created with ownership of the data directory.

### Managing the service

```bash
sudo systemctl status snmp-http-sd
sudo journalctl -u snmp-http-sd -f
sudo systemctl restart snmp-http-sd
```

---

## Configuration

All configuration is via environment variables. Edit the service unit to change them:

```bash
sudo systemctl edit snmp-http-sd
```

| Variable | Default | Description |
|---|---|---|
| `MODULE_LOOKUP_PATH` | `/opt/ddf-to-snmp-exporter/output/module_lookup.json` | DDF module lookup index |
| `DATA_FILE` | `/var/lib/snmp-http-sd/devices.json` | Device inventory persistence |
| `REFRESH_INTERVAL_HOURS` | `6` | How often to re-probe all devices for hostname changes |
| `SNMP_TIMEOUT` | `3` | SNMP timeout per device in seconds |
| `SNMP_RETRIES` | `1` | SNMP retries per device |

---

## REST API

The service exposes an interactive API doc at `http://localhost:8000/docs`.

### Add a device

```bash
curl -X POST http://localhost:8000/api/devices \
  -H "Content-Type: application/json" \
  -d '{
    "ip": "10.0.0.11",
    "community": "public",
    "auth": "public_v2",
    "labels": {"location": "dc1", "rack": "A01"}
  }'
```

Response `202 Accepted` — discovery runs in the background. Poll `GET /api/devices/10.0.0.11` to see results.

**Fields:**

| Field | Default | Description |
|---|---|---|
| `ip` | required | Device IP address |
| `community` | `public` | SNMP community string |
| `auth` | `public_v2` | snmp_exporter auth profile name |
| `module` | `null` | Manual module override (bypasses auto-discovery) |
| `labels` | `{}` | Extra Prometheus labels to attach to this target |

### Add multiple devices at once

```bash
curl -X POST http://localhost:8000/api/devices/batch \
  -H "Content-Type: application/json" \
  -d '[
    {"ip": "10.0.0.11", "community": "public"},
    {"ip": "10.0.0.12", "community": "public"},
    {"ip": "10.0.0.20", "community": "secretstring", "auth": "custom_v2"}
  ]'
```

### List all devices

```bash
curl http://localhost:8000/api/devices
```

```json
[
  {
    "ip": "10.0.0.11",
    "community": "public",
    "auth": "public_v2",
    "module_override": null,
    "module_discovered": "apc_acrd2g",
    "effective_module": "apc_acrd2g",
    "hostname": "ACRD2G-Row01",
    "sysobjid": "1.3.6.1.4.1.318.1.3.14.15",
    "sysdesc": "APC Web/SNMP Management Card",
    "labels": {"location": "dc1", "rack": "A01"},
    "added_at": "2026-05-03T10:00:00Z",
    "last_refreshed": "2026-05-03T16:00:00Z",
    "discovery_error": null,
    "ready": true
  }
]
```

### Get a single device

```bash
curl http://localhost:8000/api/devices/10.0.0.11
```

### Update a device

```bash
# Change community string (triggers a fresh probe)
curl -X PATCH http://localhost:8000/api/devices/10.0.0.11 \
  -H "Content-Type: application/json" \
  -d '{"community": "newcommunity"}'

# Set a manual module override (when auto-discovery returns the wrong module)
curl -X PATCH http://localhost:8000/api/devices/10.0.0.11 \
  -H "Content-Type: application/json" \
  -d '{"module": "apcsmartups"}'

# Clear a manual override — revert to auto-discovered module
curl -X PATCH http://localhost:8000/api/devices/10.0.0.11 \
  -H "Content-Type: application/json" \
  -d '{"module": ""}'

# Add or update labels
curl -X PATCH http://localhost:8000/api/devices/10.0.0.11 \
  -H "Content-Type: application/json" \
  -d '{"labels": {"location": "dc1", "rack": "B02"}}'
```

### Remove a device

```bash
curl -X DELETE http://localhost:8000/api/devices/10.0.0.11
```

### Force re-probe

```bash
# Single device
curl -X POST http://localhost:8000/api/devices/10.0.0.11/refresh

# All devices
curl -X POST http://localhost:8000/api/refresh
```

### Reload module lookup

```bash
curl -X POST http://localhost:8000/api/reload-lookup
```

Re-reads `module_lookup.json` from disk without restarting the service. Call this after the daily DDF sync regenerates the lookup index so that newly added modules are available immediately. Returns `200 OK` when complete.

### Health check

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "devices": {
    "total": 12,
    "ready": 11,
    "pending_discovery": 1
  },
  "config": {
    "refresh_interval_hours": 6.0,
    "snmp_timeout": 3,
    "module_lookup": "/opt/ddf-to-snmp-exporter/output/module_lookup.json"
  }
}
```

---

## Prometheus configuration

```yaml
scrape_configs:
  - job_name: snmp_devices
    http_sd_configs:
      - url: http://snmp-http-sd:8000/targets
        refresh_interval: 60s
    metrics_path: /snmp
    relabel_configs:
      # Send the device IP to snmp_exporter as the SNMP target
      - source_labels: [__address__]
        target_label: __param_target
      # Module and auth come pre-set from the http_sd labels
      - source_labels: [__param_module]
        target_label: __param_module
      - source_labels: [__param_auth]
        target_label: __param_auth
      # Route scrapes through snmp_exporter instead of hitting devices directly
      - target_label: __address__
        replacement: snmp-exporter:9116
      # Preserve the device IP as the instance label
      - source_labels: [__param_target]
        target_label: instance
      # Preserve the resolved hostname as a label
      - source_labels: [hostname]
        target_label: hostname
```

### What `/targets` returns

```json
[
  {
    "targets": ["10.0.0.11:161"],
    "labels": {
      "__param_module": "apc_acrd2g",
      "__param_auth": "public_v2",
      "hostname": "ACRD2G-Row01",
      "sysobjid": "1.3.6.1.4.1.318.1.3.14.15",
      "location": "dc1",
      "rack": "A01"
    }
  }
]
```

Devices without a resolved module are excluded from `/targets` until discovery completes or a manual override is set.

---

## Docker

### Quick start

```bash
# 1. Create a data directory and populate it
mkdir -p data
cp /path/to/ddf-to-snmp-exporter/output/module_lookup.json data/
cp /path/to/ddf-to-snmp-exporter/output/snmp.yml data/

# 2. Configure (optional — all have defaults)
cp .env.example .env
# Edit .env if you want a different port, refresh interval, etc.

# 3. Start both snmp-http-sd and snmp_exporter
docker compose up -d
```

`devices.json` is written automatically into the same `data/` directory and survives container restarts.

### Configurable parameters

Set these in `.env` (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `./data` | Host directory mounted as `/data` — put `module_lookup.json` and `snmp.yml` here |
| `SNMP_CONFIG_DIR` | `./data` | Host directory mounted into snmp_exporter — defaults to same as `DATA_DIR` |
| `PORT` | `8000` | Host port for snmp-http-sd |
| `REFRESH_INTERVAL_HOURS` | `6` | How often to re-probe all devices for hostname changes |
| `SNMP_TIMEOUT` | `3` | SNMP timeout per device in seconds |
| `SNMP_RETRIES` | `1` | SNMP retries per device |

### Reloading the module lookup without restarting

When the daily DDF sync regenerates `module_lookup.json`, tell the running container to reload it:

```bash
curl -X POST http://localhost:8000/api/reload-lookup
```

If you're running `ddf-to-snmp-exporter`'s sync service on the same host with the data directory bind-mounted, you can add this call to your sync script to make the reload automatic.

### Build the image locally

```bash
docker build -t snmp-http-sd:latest .
```

### Run without docker-compose

```bash
docker run -d \
  --name snmp-http-sd \
  -p 8000:8000 \
  -v /path/to/data:/data \
  -e REFRESH_INTERVAL_HOURS=6 \
  -e SNMP_TIMEOUT=3 \
  snmp-http-sd:latest
```

---

## Troubleshooting

### Device stays `ready: false` after adding

Auto-discovery failed. Check `discovery_error` in `GET /api/devices/{ip}`:

- `"SNMP unreachable"` — device is down, wrong IP, or firewall blocking UDP 161
- `null` error but `module_discovered: null` — SNMP works but the `sysObjectID` didn't match any DDF module

For the second case, manually set the module:
```bash
curl -X PATCH http://localhost:8000/api/devices/10.0.0.11 \
  -H "Content-Type: application/json" \
  -d '{"module": "apc_cpdu"}'
```

### Hostname not updating

The background refresh runs every 6 hours. To trigger immediately:
```bash
curl -X POST http://localhost:8000/api/devices/10.0.0.11/refresh
```

### Module lookup not found at startup

Set `MODULE_LOOKUP_PATH` to the correct path for your `module_lookup.json`, then restart the service:
```bash
sudo systemctl edit snmp-http-sd
# Add: Environment=MODULE_LOOKUP_PATH=/your/path/module_lookup.json
sudo systemctl restart snmp-http-sd
```

### Regenerating the module lookup after new DDFs

Run this on the `ddf-to-snmp-exporter` host, then reload without restarting:
```bash
python3 /opt/ddf-to-snmp-exporter/scripts/build_lookup.py \
  /opt/Schneider-Electric_SNMP-DDF-Downloader/ddf_files \
  -o /opt/ddf-to-snmp-exporter/output/module_lookup.json

# Reload without restart (systemd or Docker)
curl -X POST http://localhost:8000/api/reload-lookup
```

If you're using the daily sync service from `ddf-to-snmp-exporter`, the lookup is regenerated automatically every night.
