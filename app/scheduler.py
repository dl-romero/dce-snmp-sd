"""
Background hostname + module refresh scheduler.

Runs every REFRESH_INTERVAL_HOURS hours, iterating over all stored devices
and re-probing each one. Only updates fields that have actually changed
so the store isn't written unnecessarily.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .snmp_discovery import probe_device
from .store import DeviceStore

log = logging.getLogger(__name__)


async def refresh_all(store: DeviceStore, timeout: int = 3) -> None:
    """Re-probe every device and persist any changes."""
    devices = await store.all()
    if not devices:
        log.debug("Scheduler: no devices to refresh")
        return

    log.info("Scheduler: refreshing %d devices", len(devices))
    sem = asyncio.Semaphore(20)  # max concurrent probes during refresh

    async def _refresh_one(rec):
        async with sem:
            result = await probe_device(rec.ip, rec.community, timeout=timeout)
            now = datetime.now(timezone.utc)

            updates: dict = {"last_refreshed": now}

            if result.hostname and result.hostname != rec.hostname:
                log.info("%s: hostname changed %r → %r", rec.ip, rec.hostname, result.hostname)
                updates["hostname"] = result.hostname

            if result.sysobjid and result.sysobjid != rec.sysobjid:
                updates["sysobjid"] = result.sysobjid

            if result.sysdesc and result.sysdesc != rec.sysdesc:
                updates["sysdesc"] = result.sysdesc

            # Only update auto-discovered module; never touch a manual override
            if result.module and result.module != rec.module_discovered:
                log.info("%s: module changed %r → %r", rec.ip, rec.module_discovered, result.module)
                updates["module_discovered"] = result.module

            updates["discovery_error"] = result.error
            await store.patch(rec.ip, updates)

    await asyncio.gather(*[_refresh_one(d) for d in devices])
    log.info("Scheduler: refresh complete")


async def start_refresh_scheduler(
    store: DeviceStore,
    interval_hours: float = 6.0,
    snmp_timeout: int = 3,
) -> None:
    """Long-running coroutine — run as an asyncio Task."""
    interval_seconds = interval_hours * 3600
    log.info("Scheduler started — refresh interval: %.1fh", interval_hours)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await refresh_all(store, timeout=snmp_timeout)
        except Exception as exc:
            log.error("Scheduler: unhandled error during refresh: %s", exc)
