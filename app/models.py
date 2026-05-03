from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field, field_validator
import ipaddress


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Device models ─────────────────────────────────────────────────────────────

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
    source: str = "manual"                      # "manual" or "dce:<server-id>"
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
    source: str
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
            source=r.source,
            added_at=r.added_at,
            last_refreshed=r.last_refreshed,
            discovery_error=r.discovery_error,
            ready=r.ready,
        )


# ── DCE server models ─────────────────────────────────────────────────────────

class DceServerIn(BaseModel):
    host: str
    username: str
    password: str
    label: Optional[str] = None             # friendly name shown in UI / logs
    default_community: str = "public"       # SNMP community for imported devices
    default_auth: str = "public_v2"         # snmp_exporter auth profile for imported devices
    tls_verify: bool = False                # DCE typically uses self-signed certs


class DceServerUpdate(BaseModel):
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    label: Optional[str] = None
    default_community: Optional[str] = None
    default_auth: Optional[str] = None
    tls_verify: Optional[bool] = None
    enabled: Optional[bool] = None


class DceServerRecord(BaseModel):
    id: str = Field(default_factory=_new_id)
    host: str
    username: str
    password: str                           # stored as-is; secure the dce_servers.json file
    label: Optional[str] = None
    default_community: str = "public"
    default_auth: str = "public_v2"
    tls_verify: bool = False
    enabled: bool = True
    added_at: datetime = Field(default_factory=_utcnow)
    last_synced: Optional[datetime] = None
    last_error: Optional[str] = None
    device_count: int = 0                   # number of devices last seen in DCE


class DceServerOut(BaseModel):
    """API response shape — password is intentionally excluded."""
    id: str
    host: str
    username: str
    label: Optional[str]
    default_community: str
    default_auth: str
    tls_verify: bool
    enabled: bool
    added_at: datetime
    last_synced: Optional[datetime]
    last_error: Optional[str]
    device_count: int

    @classmethod
    def from_record(cls, r: DceServerRecord) -> "DceServerOut":
        return cls(
            id=r.id,
            host=r.host,
            username=r.username,
            label=r.label,
            default_community=r.default_community,
            default_auth=r.default_auth,
            tls_verify=r.tls_verify,
            enabled=r.enabled,
            added_at=r.added_at,
            last_synced=r.last_synced,
            last_error=r.last_error,
            device_count=r.device_count,
        )
