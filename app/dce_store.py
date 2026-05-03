"""
JSON-backed async-safe store for DCE server configurations.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import DceServerRecord

log = logging.getLogger(__name__)


def _default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Not serializable: {type(obj)}")


class DceStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._servers: dict[str, DceServerRecord] = {}

    async def load(self) -> None:
        if not self._path.exists():
            log.info("No DCE store at %s — starting empty", self._path)
            return
        async with self._lock:
            try:
                data = json.loads(self._path.read_text())
                self._servers = {
                    sid: DceServerRecord.model_validate(rec)
                    for sid, rec in data.items()
                }
                log.info("Loaded %d DCE server(s) from %s", len(self._servers), self._path)
            except Exception as exc:
                log.error("Failed to load DCE store: %s", exc)

    async def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        payload = {sid: rec.model_dump(mode="json") for sid, rec in self._servers.items()}
        tmp.write_text(json.dumps(payload, default=_default, indent=2))
        tmp.replace(self._path)

    async def add(self, record: DceServerRecord) -> DceServerRecord:
        async with self._lock:
            self._servers[record.id] = record
            await self._save()
        return record

    async def remove(self, server_id: str) -> bool:
        async with self._lock:
            if server_id not in self._servers:
                return False
            del self._servers[server_id]
            await self._save()
        return True

    async def get(self, server_id: str) -> Optional[DceServerRecord]:
        async with self._lock:
            return self._servers.get(server_id)

    async def all(self) -> list[DceServerRecord]:
        async with self._lock:
            return list(self._servers.values())

    async def patch(self, server_id: str, updates: dict) -> Optional[DceServerRecord]:
        async with self._lock:
            rec = self._servers.get(server_id)
            if rec is None:
                return None
            updated = rec.model_copy(update=updates)
            self._servers[server_id] = updated
            await self._save()
            return updated
