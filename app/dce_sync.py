"""
Hourly DCE inventory sync scheduler.

For each enabled DCE server:
  - Fetch the current device list via SOAP
  - Add devices not yet in the inventory (triggers SNMP probe for module resolution)
  - Remove devices that were added from this DCE server but are no longer there
  - Update location / hostname labels for devices already in the inventory

Devices with source="manual" are never touched by the sync.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .dce_client import fetch_devices
from .dce_store import DceStore
from .models import DeviceRecord
from .snmp_discovery import probe_device
from .store import DeviceStore

log = logging.getLogger(__name__)


def _source_tag(server_id: str) -> str:
    return f"dce:{server_id}"


async def _probe_new_device(
    ip: str,
    community: str,
    timeout: int,
    retries: int,
    store: DeviceStore,
) -> None:
    """SNMP probe that runs after a DCE device is added to the inventory."""
    result = await probe_device(ip, community, timeout=timeout, retries=retries)
    updates: dict = {
        "module_discovered": result.module,
        "discovery_error": result.error,
        "last_refreshed": datetime.now(timezone.utc),
    }
    if result.hostname:
        updates["hostname"] = result.hostname
    if result.sysobjid:
        updates["sysobjid"] = result.sysobjid
    if result.sysdesc:
        updates["sysdesc"] = result.sysdesc
    await store.patch(ip, updates)
    if result.error:
        log.warning("DCE probe %s: %s", ip, result.error)
    else:
        log.info("DCE probe %s: module=%r hostname=%r", ip, result.module, result.hostname)


async def sync_server(
    dce_store: DceStore,
    device_store: DeviceStore,
    server_id: str,
    snmp_timeout: int = 3,
    snmp_retries: int = 1,
) -> None:
    """Sync one DCE server against the device inventory."""
    server = await dce_store.get(server_id)
    if not server or not server.enabled:
        return

    label = server.label or server.host
    log.info("Data Center Expert sync: %s (%s)", label, server.host)
    source_tag = _source_tag(server_id)

    try:
        dce_devices = await fetch_devices(
            server.host, server.username, server.password, server.tls_verify
        )
    except Exception as exc:
        log.error("Data Center Expert sync: failed to reach %s: %s", server.host, exc)
        await dce_store.patch(server_id, {
            "last_error": str(exc),
            "last_synced": datetime.now(timezone.utc),
        })
        return

    dce_ips = {d.ip for d in dce_devices}

    # ── Remove stale DCE-sourced devices ──────────────────────────────────────
    for rec in await device_store.all():
        if rec.source == source_tag and rec.ip not in dce_ips:
            log.info("Data Center Expert sync: removing %s (no longer in %s)", rec.ip, server.host)
            await device_store.remove(rec.ip)

    # ── Add new / refresh existing ────────────────────────────────────────────
    added = 0
    for dev in dce_devices:
        existing = await device_store.get(dev.ip)

        if existing is not None and existing.source == "manual":
            continue  # never overwrite manually managed entries

        if existing is None:
            labels: dict[str, str] = {}
            if dev.name:
                labels["dce_name"] = dev.name
            if dev.location:
                labels["location"] = dev.location
            if dev.model_name:
                labels["model"] = dev.model_name
            if server.label:
                labels["dce_server"] = server.label

            record = DeviceRecord(
                ip=dev.ip,
                community=server.default_community,
                auth=server.default_auth,
                hostname=dev.hostname,
                source=source_tag,
                labels=labels,
            )
            await device_store.add(record)
            added += 1
            asyncio.create_task(
                _probe_new_device(dev.ip, server.default_community, snmp_timeout, snmp_retries, device_store)
            )
        else:
            # Already known — patch labels/hostname from DCE if changed
            updates: dict = {}
            if dev.hostname and dev.hostname != existing.hostname:
                updates["hostname"] = dev.hostname
            if dev.location:
                merged = {**existing.labels, "location": dev.location}
                if merged != existing.labels:
                    updates["labels"] = merged
            if server.label:
                merged = {**existing.labels, "dce_server": server.label}
                if merged != existing.labels:
                    updates["labels"] = {**updates.get("labels", existing.labels), "dce_server": server.label}
            if updates:
                await device_store.patch(dev.ip, updates)

    await dce_store.patch(server_id, {
        "last_synced": datetime.now(timezone.utc),
        "last_error": None,
        "device_count": len(dce_ips),
    })
    log.info(
        "Data Center Expert sync: %s done — %d device(s) in DCE, %d newly added",
        server.host, len(dce_ips), added,
    )


async def sync_all_servers(
    dce_store: DceStore,
    device_store: DeviceStore,
    snmp_timeout: int = 3,
    snmp_retries: int = 1,
) -> None:
    """Sync all enabled Data Center Expert servers concurrently."""
    servers = [s for s in await dce_store.all() if s.enabled]
    if not servers:
        log.debug("Data Center Expert sync: no enabled servers configured")
        return
    log.info("Data Center Expert sync: running against %d server(s)", len(servers))
    await asyncio.gather(
        *[
            sync_server(dce_store, device_store, s.id, snmp_timeout, snmp_retries)
            for s in servers
        ],
        return_exceptions=True,
    )


async def start_dce_scheduler(
    dce_store: DceStore,
    device_store: DeviceStore,
    interval_hours: float = 1.0,
    snmp_timeout: int = 3,
    snmp_retries: int = 1,
) -> None:
    """Long-running coroutine — run as an asyncio Task."""
    log.info("DCE scheduler started — sync interval: %.1fh", interval_hours)
    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            await sync_all_servers(dce_store, device_store, snmp_timeout, snmp_retries)
        except Exception as exc:
            log.error("DCE scheduler: unhandled error: %s", exc)
