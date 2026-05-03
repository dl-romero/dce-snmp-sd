"""
snmp-http-sd — Prometheus HTTP service discovery for snmp_exporter

Environment variables:
  DATA_FILE                Path to persist device inventory   (default: devices.json)
  DCE_SERVERS_FILE         Path to persist DCE server configs (default: <DATA_FILE dir>/dce_servers.json)
  MODULE_LOOKUP_PATH       Path to module_lookup.json from ddf-to-snmp-exporter
  REFRESH_INTERVAL_HOURS   Hostname refresh interval in hours (default: 6)
  DCE_SYNC_INTERVAL_HOURS  DCE inventory sync interval in hours (default: 1)
  SNMP_TIMEOUT             SNMP timeout per device in seconds (default: 3)
  SNMP_RETRIES             SNMP retries per device (default: 1)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, status
from fastapi.responses import JSONResponse

from .models import (
    DeviceIn, DeviceOut, DeviceRecord, DeviceUpdate,
    DceServerIn, DceServerOut, DceServerRecord, DceServerUpdate,
)
from .store import DeviceStore
from .dce_store import DceStore
from .snmp_discovery import configure_lookup, probe_device, reload_lookup
from .scheduler import start_refresh_scheduler, refresh_all
from .dce_sync import start_dce_scheduler, sync_all_servers, sync_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DATA_FILE = os.getenv("DATA_FILE", "devices.json")
DCE_SERVERS_FILE = os.getenv(
    "DCE_SERVERS_FILE",
    str(Path(DATA_FILE).parent / "dce_servers.json"),
)
MODULE_LOOKUP_PATH = os.getenv(
    "MODULE_LOOKUP_PATH",
    "/opt/ddf-to-snmp-exporter/output/module_lookup.json",
)
REFRESH_INTERVAL_HOURS = float(os.getenv("REFRESH_INTERVAL_HOURS", "6"))
DCE_SYNC_INTERVAL_HOURS = float(os.getenv("DCE_SYNC_INTERVAL_HOURS", "1"))
SNMP_TIMEOUT = int(os.getenv("SNMP_TIMEOUT", "3"))
SNMP_RETRIES = int(os.getenv("SNMP_RETRIES", "1"))

# ── App setup ─────────────────────────────────────────────────────────────────

store = DeviceStore(DATA_FILE)
dce_store = DceStore(DCE_SERVERS_FILE)
_scheduler_task: Optional[asyncio.Task] = None
_dce_scheduler_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task, _dce_scheduler_task
    configure_lookup(MODULE_LOOKUP_PATH)
    await store.load()
    await dce_store.load()
    _scheduler_task = asyncio.create_task(
        start_refresh_scheduler(store, REFRESH_INTERVAL_HOURS, SNMP_TIMEOUT)
    )
    _dce_scheduler_task = asyncio.create_task(
        start_dce_scheduler(dce_store, store, DCE_SYNC_INTERVAL_HOURS, SNMP_TIMEOUT, SNMP_RETRIES)
    )
    log.info(
        "snmp-http-sd started | data=%s | lookup=%s | refresh=%.1fh | dce_sync=%.1fh",
        DATA_FILE, MODULE_LOOKUP_PATH, REFRESH_INTERVAL_HOURS, DCE_SYNC_INTERVAL_HOURS,
    )
    yield
    _scheduler_task.cancel()
    _dce_scheduler_task.cancel()


app = FastAPI(
    title="SNMP HTTP SD",
    description="Prometheus HTTP service discovery for snmp_exporter, backed by DDF module lookup",
    version="1.1.0",
    lifespan=lifespan,
)


# ── Background probe helper ───────────────────────────────────────────────────

async def _run_probe(ip: str, community: str) -> None:
    log.info("%s: starting discovery probe", ip)
    result = await probe_device(ip, community, timeout=SNMP_TIMEOUT, retries=SNMP_RETRIES)
    now = datetime.now(timezone.utc)
    await store.patch(ip, {
        "hostname": result.hostname,
        "sysobjid": result.sysobjid,
        "sysdesc": result.sysdesc,
        "module_discovered": result.module,
        "discovery_error": result.error,
        "last_refreshed": now,
    })
    if result.error:
        log.warning("%s: probe finished with error: %s", ip, result.error)
    else:
        log.info("%s: probe done — hostname=%r module=%r", ip, result.hostname, result.module)


# ── Service routes ────────────────────────────────────────────────────────────

@app.get("/health", tags=["service"])
async def health():
    """Service health, device inventory stats, and DCE server status."""
    devices = await store.all()
    dce_servers = await dce_store.all()
    ready = sum(1 for d in devices if d.ready)
    return {
        "status": "ok",
        "devices": {
            "total": len(devices),
            "ready": ready,
            "pending_discovery": len(devices) - ready,
        },
        "dce_servers": {
            "total": len(dce_servers),
            "enabled": sum(1 for s in dce_servers if s.enabled),
        },
        "config": {
            "refresh_interval_hours": REFRESH_INTERVAL_HOURS,
            "dce_sync_interval_hours": DCE_SYNC_INTERVAL_HOURS,
            "snmp_timeout": SNMP_TIMEOUT,
            "module_lookup": MODULE_LOOKUP_PATH,
        },
    }


@app.post("/api/reload-lookup", tags=["service"])
async def reload_lookup_endpoint():
    """
    Reload module_lookup.json from disk without restarting the service.

    Call this after the daily DDF sync regenerates the lookup index so that
    newly added modules are available immediately.
    """
    reload_lookup()
    return {"status": "ok", "message": f"Module lookup reloaded from {MODULE_LOOKUP_PATH}"}


@app.get("/targets", tags=["prometheus"], response_class=JSONResponse)
async def http_sd_targets():
    """
    Prometheus http_sd endpoint.

    Returns targets only for devices that have a resolved module.
    Devices still awaiting discovery are omitted.
    """
    devices = await store.all()
    targets = []
    for d in devices:
        if not d.effective_module:
            continue
        labels = {
            "__param_module": d.effective_module,
            "__param_auth": d.auth,
        }
        if d.hostname:
            labels["hostname"] = d.hostname
        if d.sysobjid:
            labels["sysobjid"] = d.sysobjid
        labels.update(d.labels)
        targets.append({"targets": [f"{d.ip}:161"], "labels": labels})
    return targets


# ── Device routes ─────────────────────────────────────────────────────────────

@app.get("/api/devices", tags=["devices"], response_model=list[DeviceOut])
async def list_devices():
    """List all managed devices with their discovery status."""
    return [DeviceOut.from_record(d) for d in await store.all()]


@app.get("/api/devices/{ip}", tags=["devices"], response_model=DeviceOut)
async def get_device(ip: str):
    rec = await store.get(ip)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Device {ip} not found")
    return DeviceOut.from_record(rec)


@app.post("/api/devices", tags=["devices"], status_code=status.HTTP_202_ACCEPTED)
async def add_device(device: DeviceIn, background_tasks: BackgroundTasks):
    """
    Add a device. Discovery runs in the background — the device appears in
    /targets once hostname and module have been resolved.
    """
    if await store.get(device.ip):
        raise HTTPException(
            status_code=409,
            detail=f"Device {device.ip} already exists. Use PATCH to update it.",
        )
    record = DeviceRecord(
        ip=device.ip,
        community=device.community,
        auth=device.auth,
        module_override=device.module or None,
        labels=device.labels,
        source="manual",
    )
    await store.add(record)
    background_tasks.add_task(_run_probe, device.ip, device.community)
    return {
        "status": "accepted",
        "message": f"Device {device.ip} added. Discovery running in background.",
        "ip": device.ip,
    }


@app.post("/api/devices/batch", tags=["devices"], status_code=status.HTTP_202_ACCEPTED)
async def add_devices_batch(devices: list[DeviceIn], background_tasks: BackgroundTasks):
    """Add multiple devices at once."""
    added, skipped = [], []
    for device in devices:
        if await store.get(device.ip):
            skipped.append(device.ip)
            continue
        record = DeviceRecord(
            ip=device.ip,
            community=device.community,
            auth=device.auth,
            module_override=device.module or None,
            labels=device.labels,
            source="manual",
        )
        await store.add(record)
        background_tasks.add_task(_run_probe, device.ip, device.community)
        added.append(device.ip)
    return {"status": "accepted", "added": added, "skipped_already_exist": skipped}


@app.patch("/api/devices/{ip}", tags=["devices"], response_model=DeviceOut)
async def update_device(ip: str, update: DeviceUpdate, background_tasks: BackgroundTasks):
    """
    Update community, auth, module override, or labels.
    Set module to "" to clear a manual override and revert to auto-discovery.
    Setting a new community triggers a fresh background probe.
    """
    rec = await store.get(ip)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Device {ip} not found")

    updates: dict = {}
    reprobe = False

    if update.community is not None and update.community != rec.community:
        updates["community"] = update.community
        reprobe = True
    if update.auth is not None:
        updates["auth"] = update.auth
    if update.module is not None:
        updates["module_override"] = update.module if update.module != "" else None
    if update.labels is not None:
        updates["labels"] = update.labels

    updated = await store.patch(ip, updates)
    if reprobe:
        background_tasks.add_task(_run_probe, ip, update.community or rec.community)
    return DeviceOut.from_record(updated)


@app.delete("/api/devices/{ip}", tags=["devices"], status_code=status.HTTP_204_NO_CONTENT)
async def remove_device(ip: str):
    """Remove a device from the inventory."""
    if not await store.remove(ip):
        raise HTTPException(status_code=404, detail=f"Device {ip} not found")


@app.post("/api/devices/{ip}/refresh", tags=["devices"], status_code=status.HTTP_202_ACCEPTED)
async def refresh_device(ip: str, background_tasks: BackgroundTasks):
    """Force an immediate re-probe for a single device."""
    rec = await store.get(ip)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Device {ip} not found")
    background_tasks.add_task(_run_probe, ip, rec.community)
    return {"status": "accepted", "message": f"Refresh probe queued for {ip}"}


@app.post("/api/refresh", tags=["devices"], status_code=status.HTTP_202_ACCEPTED)
async def refresh_all_devices(background_tasks: BackgroundTasks):
    """Force an immediate re-probe for all devices."""
    devices = await store.all()
    background_tasks.add_task(refresh_all, store, SNMP_TIMEOUT)
    return {"status": "accepted", "message": f"Refresh queued for {len(devices)} devices"}


# ── DCE server routes ─────────────────────────────────────────────────────────

@app.get("/api/dce", tags=["dce"], response_model=list[DceServerOut])
async def list_dce_servers():
    """List all configured DCE servers. Passwords are not returned."""
    return [DceServerOut.from_record(s) for s in await dce_store.all()]


@app.post("/api/dce/sync", tags=["dce"], status_code=status.HTTP_202_ACCEPTED)
async def sync_all_dce(background_tasks: BackgroundTasks):
    """Force an immediate sync from all enabled DCE servers."""
    servers = [s for s in await dce_store.all() if s.enabled]
    background_tasks.add_task(sync_all_servers, dce_store, store, SNMP_TIMEOUT, SNMP_RETRIES)
    return {"status": "accepted", "message": f"Sync queued for {len(servers)} DCE server(s)"}


@app.post("/api/dce", tags=["dce"], response_model=DceServerOut, status_code=status.HTTP_201_CREATED)
async def add_dce_server(server: DceServerIn, background_tasks: BackgroundTasks):
    """
    Add a DCE server. An initial sync runs in the background immediately.
    The server's device inventory will also sync automatically every hour.
    """
    record = DceServerRecord(
        host=server.host,
        username=server.username,
        password=server.password,
        label=server.label,
        default_community=server.default_community,
        default_auth=server.default_auth,
        tls_verify=server.tls_verify,
    )
    await dce_store.add(record)
    background_tasks.add_task(sync_server, dce_store, store, record.id, SNMP_TIMEOUT, SNMP_RETRIES)
    log.info("DCE server added: %s (%s) — initial sync queued", record.label or record.host, record.host)
    return DceServerOut.from_record(record)


@app.get("/api/dce/{server_id}", tags=["dce"], response_model=DceServerOut)
async def get_dce_server(server_id: str):
    rec = await dce_store.get(server_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"DCE server {server_id} not found")
    return DceServerOut.from_record(rec)


@app.patch("/api/dce/{server_id}", tags=["dce"], response_model=DceServerOut)
async def update_dce_server(server_id: str, update: DceServerUpdate):
    """Update DCE server config. Changes take effect on the next sync cycle."""
    rec = await dce_store.get(server_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"DCE server {server_id} not found")

    updates = {k: v for k, v in update.model_dump().items() if v is not None}
    updated = await dce_store.patch(server_id, updates)
    return DceServerOut.from_record(updated)


@app.delete("/api/dce/{server_id}", tags=["dce"], status_code=status.HTTP_204_NO_CONTENT)
async def remove_dce_server(server_id: str):
    """
    Remove a DCE server and all devices that were imported from it.
    Manually added devices are not affected.
    """
    rec = await dce_store.get(server_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"DCE server {server_id} not found")

    source_tag = f"dce:{server_id}"
    removed_devices = 0
    for device in await store.all():
        if device.source == source_tag:
            await store.remove(device.ip)
            removed_devices += 1

    await dce_store.remove(server_id)
    log.info(
        "DCE server %s removed — %d device(s) also removed",
        rec.label or rec.host, removed_devices,
    )


@app.post("/api/dce/{server_id}/sync", tags=["dce"], status_code=status.HTTP_202_ACCEPTED)
async def sync_dce_server(server_id: str, background_tasks: BackgroundTasks):
    """Force an immediate sync from a specific DCE server."""
    rec = await dce_store.get(server_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"DCE server {server_id} not found")
    background_tasks.add_task(sync_server, dce_store, store, server_id, SNMP_TIMEOUT, SNMP_RETRIES)
    return {
        "status": "accepted",
        "message": f"Sync queued for DCE server {rec.label or rec.host}",
    }
