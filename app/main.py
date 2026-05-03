"""
snmp-http-sd — Prometheus HTTP service discovery for snmp_exporter

Environment variables:
  DATA_FILE              Path to persist device list  (default: devices.json)
  MODULE_LOOKUP_PATH     Path to module_lookup.json from ddf-to-snmp-exporter
                         (default: /opt/ddf-to-snmp-exporter/output/module_lookup.json)
  REFRESH_INTERVAL_HOURS Hostname refresh interval in hours (default: 6)
  SNMP_TIMEOUT           SNMP timeout per device in seconds (default: 3)
  SNMP_RETRIES           SNMP retries per device (default: 1)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, status
from fastapi.responses import JSONResponse

from .models import DeviceIn, DeviceOut, DeviceRecord, DeviceUpdate
from .store import DeviceStore
from .snmp_discovery import configure_lookup, probe_device, resolve_hostname
from .scheduler import start_refresh_scheduler, refresh_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DATA_FILE = os.getenv("DATA_FILE", "devices.json")
MODULE_LOOKUP_PATH = os.getenv(
    "MODULE_LOOKUP_PATH",
    "/opt/ddf-to-snmp-exporter/output/module_lookup.json",
)
REFRESH_INTERVAL_HOURS = float(os.getenv("REFRESH_INTERVAL_HOURS", "6"))
SNMP_TIMEOUT = int(os.getenv("SNMP_TIMEOUT", "3"))
SNMP_RETRIES = int(os.getenv("SNMP_RETRIES", "1"))

# ── App setup ─────────────────────────────────────────────────────────────────

store = DeviceStore(DATA_FILE)
_scheduler_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task
    configure_lookup(MODULE_LOOKUP_PATH)
    await store.load()
    _scheduler_task = asyncio.create_task(
        start_refresh_scheduler(store, REFRESH_INTERVAL_HOURS, SNMP_TIMEOUT)
    )
    log.info(
        "snmp-http-sd started | data=%s | lookup=%s | refresh=%.1fh",
        DATA_FILE, MODULE_LOOKUP_PATH, REFRESH_INTERVAL_HOURS,
    )
    yield
    _scheduler_task.cancel()


app = FastAPI(
    title="SNMP HTTP SD",
    description="Prometheus HTTP service discovery for snmp_exporter, backed by DDF module lookup",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Background probe helper ───────────────────────────────────────────────────

async def _run_probe(ip: str, community: str) -> None:
    """Probe a device and persist the results. Runs as a background task."""
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
        log.info(
            "%s: probe done — hostname=%r module=%r",
            ip, result.hostname, result.module,
        )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["service"])
async def health():
    """Service health and device inventory stats."""
    devices = await store.all()
    ready = [d for d in devices if d.ready]
    pending = [d for d in devices if not d.ready]
    return {
        "status": "ok",
        "devices": {
            "total": len(devices),
            "ready": len(ready),
            "pending_discovery": len(pending),
        },
        "config": {
            "refresh_interval_hours": REFRESH_INTERVAL_HOURS,
            "snmp_timeout": SNMP_TIMEOUT,
            "module_lookup": MODULE_LOOKUP_PATH,
        },
    }


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

        targets.append({
            "targets": [f"{d.ip}:161"],
            "labels": labels,
        })
    return targets


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
    Add a device. Discovery runs in the background — the device appears
    in /targets once hostname and module have been resolved.
    """
    existing = await store.get(device.ip)
    if existing:
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
    )
    await store.add(record)
    background_tasks.add_task(_run_probe, device.ip, device.community)

    return {
        "status": "accepted",
        "message": f"Device {device.ip} added. Discovery running in background.",
        "ip": device.ip,
    }


@app.post(
    "/api/devices/batch",
    tags=["devices"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def add_devices_batch(devices: list[DeviceIn], background_tasks: BackgroundTasks):
    """Add multiple devices at once."""
    added, skipped = [], []
    for device in devices:
        existing = await store.get(device.ip)
        if existing:
            skipped.append(device.ip)
            continue
        record = DeviceRecord(
            ip=device.ip,
            community=device.community,
            auth=device.auth,
            module_override=device.module or None,
            labels=device.labels,
        )
        await store.add(record)
        background_tasks.add_task(_run_probe, device.ip, device.community)
        added.append(device.ip)

    return {
        "status": "accepted",
        "added": added,
        "skipped_already_exist": skipped,
    }


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
        community = update.community or rec.community
        background_tasks.add_task(_run_probe, ip, community)

    return DeviceOut.from_record(updated)


@app.delete("/api/devices/{ip}", tags=["devices"], status_code=status.HTTP_204_NO_CONTENT)
async def remove_device(ip: str):
    """Remove a device from the inventory."""
    removed = await store.remove(ip)
    if not removed:
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
    return {
        "status": "accepted",
        "message": f"Refresh queued for {len(devices)} devices",
    }
