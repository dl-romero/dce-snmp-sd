from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field, field_validator
import ipaddress


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeviceIn(BaseModel):
    ip: str
    community: str = "public"
    auth: str = "public_v2"
    module: Optional[str] = None        # manual module override; None = auto-discover
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, v: str) -> str:
        try:
            ipaddress.ip_address(v)
        except ValueError:
            raise ValueError(f"Invalid IP address: {v!r}")
        return v


class DeviceUpdate(BaseModel):
    community: Optional[str] = None
    auth: Optional[str] = None
    module: Optional[str] = None        # set to "" to clear a manual override
    labels: Optional[dict[str, str]] = None


class DeviceRecord(BaseModel):
    ip: str
    community: str = "public"
    auth: str = "public_v2"
    module_override: Optional[str] = None       # manually set by user
    module_discovered: Optional[str] = None     # resolved from DDF lookup
    hostname: Optional[str] = None
    sysobjid: Optional[str] = None
    sysdesc: Optional[str] = None
    labels: dict[str, str] = Field(default_factory=dict)
    added_at: datetime = Field(default_factory=_utcnow)
    last_refreshed: Optional[datetime] = None
    discovery_error: Optional[str] = None       # last error message if probe failed

    @property
    def effective_module(self) -> Optional[str]:
        """Module override wins; falls back to auto-discovered."""
        return self.module_override or self.module_discovered

    @property
    def ready(self) -> bool:
        """True when the device has enough info to appear in /targets."""
        return self.effective_module is not None


class DeviceOut(BaseModel):
    """API response shape for a single device."""
    ip: str
    community: str
    auth: str
    module_override: Optional[str]
    module_discovered: Optional[str]
    effective_module: Optional[str]
    hostname: Optional[str]
    sysobjid: Optional[str]
    sysdesc: Optional[str]
    labels: dict[str, str]
    added_at: datetime
    last_refreshed: Optional[datetime]
    discovery_error: Optional[str]
    ready: bool

    @classmethod
    def from_record(cls, r: DeviceRecord) -> "DeviceOut":
        return cls(
            ip=r.ip,
            community=r.community,
            auth=r.auth,
            module_override=r.module_override,
            module_discovered=r.module_discovered,
            effective_module=r.effective_module,
            hostname=r.hostname,
            sysobjid=r.sysobjid,
            sysdesc=r.sysdesc,
            labels=r.labels,
            added_at=r.added_at,
            last_refreshed=r.last_refreshed,
            discovery_error=r.discovery_error,
            ready=r.ready,
        )
