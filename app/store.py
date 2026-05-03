"""
JSON-backed async-safe device store.

All mutations go through the store so every write is immediately persisted
and the in-memory dict is always consistent with the file on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import DeviceRecord

log = logging.getLogger(__name__)


def _default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Not serializable: {type(obj)}")


class DeviceStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._devices: dict[str, DeviceRecord] = {}

    # ── Persistence ───────────────────────────────────────────────────────────

    async def load(self) -> None:
        if not self._path.exists():
            log.info("No device store found at %s — starting empty", self._path)
            return
        async with self._lock:
            try:
                data = json.loads(self._path.read_text())
                self._devices = {
                    ip: DeviceRecord.model_validate(rec)
                    for ip, rec in data.items()
                }
                log.info("Loaded %d devices from %s", len(self._devices), self._path)
            except Exception as exc:
                log.error("Failed to load device store: %s", exc)

    async def _save(self) -> None:
        """Must be called with self._lock held."""
        tmp = self._path.with_suffix(".tmp")
        payload = {
            ip: rec.model_dump(mode="json")
            for ip, rec in self._devices.items()
        }
        tmp.write_text(json.dumps(payload, default=_default, indent=2))
        tmp.replace(self._path)

    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def add(self, record: DeviceRecord) -> bool:
        """Add or replace a device. Returns True if it was new."""
        async with self._lock:
            is_new = record.ip not in self._devices
            self._devices[record.ip] = record
            await self._save()
        return is_new

    async def remove(self, ip: str) -> bool:
        """Remove a device. Returns True if it existed."""
        async with self._lock:
            if ip not in self._devices:
                return False
            del self._devices[ip]
            await self._save()
        return True

    async def get(self, ip: str) -> Optional[DeviceRecord]:
        async with self._lock:
            return self._devices.get(ip)

    async def all(self) -> list[DeviceRecord]:
        async with self._lock:
            return list(self._devices.values())

    async def update(self, ip: str, **fields) -> Optional[DeviceRecord]:
        """Patch specific fields on an existing record."""
        async with self._lock:
            rec = self._devices.get(ip)
            if rec is None:
                return None
            updated = rec.model_copy(update={k: v for k, v in fields.items() if v is not None or k in fields})
            self._devices[ip] = updated
            await self._save()
            return updated

    async def patch(self, ip: str, updates: dict) -> Optional[DeviceRecord]:
        """Apply a dict of field updates (allows explicit None values)."""
        async with self._lock:
            rec = self._devices.get(ip)
            if rec is None:
                return None
            updated = rec.model_copy(update=updates)
            self._devices[ip] = updated
            await self._save()
            return updated
