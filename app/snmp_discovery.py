"""
SNMP-based device discovery.

Resolves:
  - hostname   via sysName OID (falls back to reverse DNS)
  - module     via module_lookup.json (sysObjectID exact → prefix → OID probe)
  - sysobjid   via sysObjectID OID
  - sysdesc    via sysDescr OID
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SYSOID_OID  = "1.3.6.1.2.1.1.2.0"
SYSNAME_OID = "1.3.6.1.2.1.1.5.0"
SYSDESC_OID = "1.3.6.1.2.1.1.1.0"

_lookup: Optional[dict] = None
_lookup_path: Optional[Path] = None


def configure_lookup(path: str | Path) -> None:
    global _lookup, _lookup_path
    _lookup_path = Path(path)
    _reload_lookup()


def _reload_lookup() -> None:
    global _lookup
    if _lookup_path and _lookup_path.exists():
        try:
            _lookup = json.loads(_lookup_path.read_text())
            log.info("Loaded module lookup: %d modules", len(_lookup.get("modules", {})))
        except Exception as exc:
            log.error("Failed to load module lookup: %s", exc)
    else:
        log.warning("Module lookup file not found: %s", _lookup_path)
        _lookup = None


def reload_lookup() -> None:
    """Reload the lookup file from disk (call after the daily DDF sync updates it)."""
    _reload_lookup()


# ── SNMP GET ──────────────────────────────────────────────────────────────────

async def _snmp_get(
    host: str,
    oids: list[str],
    community: str,
    port: int = 161,
    timeout: int = 3,
    retries: int = 1,
) -> dict[str, str]:
    try:
        from pysnmp.hlapi.asyncio import (
            SnmpEngine, CommunityData, UdpTransportTarget,
            ContextData, ObjectType, ObjectIdentity, get_cmd,
        )
    except ImportError:
        log.error("pysnmp not installed")
        return {}

    try:
        transport = await UdpTransportTarget.create(
            (host, port), timeout=timeout, retries=retries
        )
    except Exception as exc:
        log.debug("%s: transport error: %s", host, exc)
        return {}

    var_binds = [ObjectType(ObjectIdentity(oid)) for oid in oids]
    try:
        err_ind, err_status, _idx, result = await get_cmd(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            transport,
            ContextData(),
            *var_binds,
        )
    except Exception as exc:
        log.debug("%s: SNMP error: %s", host, exc)
        return {}

    if err_ind or err_status:
        log.debug("%s: %s %s", host, err_ind or "", err_status or "")
        return {}

    out: dict[str, str] = {}
    for binding in result:
        oid_str, val = binding
        key = str(oid_str).lstrip(".")
        out[key] = str(val)
        out[key.removesuffix(".0")] = str(val)
    return out


# ── Hostname ──────────────────────────────────────────────────────────────────

def _rdns(ip: str) -> Optional[str]:
    try:
        name = socket.gethostbyaddr(ip)[0]
        return name if name != ip else None
    except Exception:
        return None


async def resolve_hostname(ip: str, community: str, timeout: int = 3) -> Optional[str]:
    """Return sysName from SNMP, or reverse DNS, or None."""
    values = await _snmp_get(ip, [SYSNAME_OID], community, timeout=timeout)
    name = values.get(SYSNAME_OID.rstrip(".0")) or values.get(SYSNAME_OID)
    if name and name not in ("", "0", "noSuchObject", "noSuchInstance"):
        return name.strip()
    # Fallback: reverse DNS (run in thread pool to avoid blocking the event loop)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _rdns, ip)


# ── Module resolution ─────────────────────────────────────────────────────────

def _norm(oid: str) -> str:
    return oid.strip().lstrip(".")


def _resolve_by_sysobjid(sysobjid: str) -> list[str]:
    if not _lookup:
        return []
    normed = _norm(sysobjid)

    # Exact match
    hits = _lookup.get("sysobjid_index", {}).get(normed, [])
    if hits:
        return hits

    # Prefix/wildcard match (longest prefix wins)
    best: list[str] = []
    best_len = 0
    for prefix, mods in _lookup.get("sysobjid_prefix_index", {}).items():
        if normed.startswith(prefix) and len(prefix) > best_len:
            best = mods
            best_len = len(prefix)
    return best


async def _resolve_by_oid_probe(
    ip: str, community: str, timeout: int
) -> list[str]:
    if not _lookup:
        return []
    req_index = _lookup.get("reqoid_index", {})
    # Pick the 30 most discriminating OIDs (fewest module candidates)
    probe_oids = sorted(req_index.keys(), key=lambda o: len(req_index[o]))[:30]
    values = await _snmp_get(ip, probe_oids, community, timeout=timeout)
    if not values:
        return []

    scores: dict[str, int] = {}
    for oid in probe_oids:
        normed = _norm(oid)
        exists = any(
            v.startswith(normed) or normed.startswith(v)
            for v in values
        )
        if exists:
            for mid in req_index.get(oid, []):
                scores[mid] = scores.get(mid, 0) + 1

    return sorted(scores, key=lambda m: scores[m], reverse=True)


async def resolve_module(
    ip: str, sysobjid: str, community: str, timeout: int = 3
) -> Optional[str]:
    """Three-tier module resolution. Returns the best module id or None."""
    candidates = _resolve_by_sysobjid(sysobjid)
    if not candidates:
        candidates = await _resolve_by_oid_probe(ip, community, timeout)
    if not candidates:
        return None
    return candidates[0]


# ── Full device probe ─────────────────────────────────────────────────────────

class ProbeResult:
    def __init__(
        self,
        hostname: Optional[str],
        sysobjid: Optional[str],
        sysdesc: Optional[str],
        module: Optional[str],
        error: Optional[str] = None,
    ):
        self.hostname = hostname
        self.sysobjid = sysobjid
        self.sysdesc = sysdesc
        self.module = module
        self.error = error


async def probe_device(
    ip: str,
    community: str = "public",
    timeout: int = 3,
    retries: int = 1,
) -> ProbeResult:
    """
    Full probe: fetch sysName, sysObjectID, sysDescr in one query,
    then resolve the module. Returns a ProbeResult even on failure.
    """
    oids = [SYSOID_OID, SYSNAME_OID, SYSDESC_OID]
    values = await _snmp_get(ip, oids, community, timeout=timeout, retries=retries)

    if not values:
        # Try reverse DNS as last resort for hostname
        loop = asyncio.get_event_loop()
        rdns = await loop.run_in_executor(None, _rdns, ip)
        return ProbeResult(
            hostname=rdns,
            sysobjid=None,
            sysdesc=None,
            module=None,
            error="SNMP unreachable",
        )

    def _get(*keys: str) -> Optional[str]:
        for k in keys:
            v = values.get(k)
            if v and v not in ("noSuchObject", "noSuchInstance", ""):
                return v.strip()
        return None

    raw_sysobjid = _norm(SYSOID_OID)
    sysobjid = _get(raw_sysobjid, raw_sysobjid + ".0")

    raw_sysname = _norm(SYSNAME_OID)
    hostname = _get(raw_sysname, raw_sysname + ".0")
    if not hostname:
        loop = asyncio.get_event_loop()
        hostname = await loop.run_in_executor(None, _rdns, ip)

    raw_sysdesc = _norm(SYSDESC_OID)
    sysdesc = _get(raw_sysdesc, raw_sysdesc + ".0")
    if sysdesc:
        sysdesc = sysdesc[:120]

    module: Optional[str] = None
    if sysobjid:
        module = await resolve_module(ip, sysobjid, community, timeout)

    return ProbeResult(
        hostname=hostname,
        sysobjid=sysobjid,
        sysdesc=sysdesc,
        module=module,
        error=None if (hostname or sysobjid) else "No SNMP data returned",
    )
