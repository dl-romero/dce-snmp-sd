"""
DCE (Data Center Expert) SOAP client.

Connects to a Schneider Electric / APC Data Center Expert server and returns
the list of monitored devices.

SOAP endpoint:  https://<host>/dce-api/services/iManageDevice
WSDL:           https://<host>/dce-api/services/iManageDevice?wsdl

If your DCE version uses different method names or a different namespace,
update the constants in the "DCE API constants" section below.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# ── DCE API constants — adjust for your DCE version ───────────────────────────

_ENDPOINT = "https://{host}/dce-api/services/iManageDevice"
_SOAP_NS = "http://www.apc.com/dce/services/manageDevice/"

_NS_DECL = (
    'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
    f'xmlns:man="{_SOAP_NS}"'
)


# ── Public types ──────────────────────────────────────────────────────────────

@dataclass
class DceDevice:
    ip: str
    hostname: Optional[str]
    location: Optional[str]
    sysobjid: Optional[str]
    node_id: Optional[str]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _envelope(body: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope {_NS_DECL}>'
        f"<soapenv:Header/>"
        f"<soapenv:Body>{body}</soapenv:Body>"
        f"</soapenv:Envelope>"
    ).encode("utf-8")


def _ssl_ctx(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _post(url: str, body: bytes, verify: bool) -> ET.Element:
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "text/xml; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=_ssl_ctx(verify), timeout=30) as resp:
        return ET.fromstring(resp.read())


def _find_text(element: ET.Element, *tags: str) -> Optional[str]:
    """Search for any of the given tag names (with and without namespace)."""
    for tag in tags:
        for candidate in (f"{{{_SOAP_NS}}}{tag}", tag):
            found = element.find(f".//{candidate}")
            if found is not None and found.text:
                return found.text.strip() or None
    return None


def _child_text(element: ET.Element, *tags: str) -> Optional[str]:
    """Like _find_text but only looks at direct children."""
    for tag in tags:
        for candidate in (f"{{{_SOAP_NS}}}{tag}", tag):
            found = element.find(candidate)
            if found is not None and found.text:
                return found.text.strip() or None
    return None


# ── SOAP operations ───────────────────────────────────────────────────────────

def _fetch_sync(host: str, username: str, password: str, verify_tls: bool) -> list[DceDevice]:
    """Blocking: open session → get nodes → close session."""
    url = _ENDPOINT.format(host=host)

    # 1. Open session
    login_resp = _post(url, _envelope(
        "<man:openSession>"
        f"<man:userName>{username}</man:userName>"
        f"<man:password>{password}</man:password>"
        "</man:openSession>"
    ), verify_tls)

    session_id = _find_text(login_resp, "return", "sessionId", "openSessionReturn")
    if not session_id:
        raise RuntimeError(f"DCE login failed — no session ID returned by {host}")

    log.debug("%s: session opened (%s…)", host, session_id[:8])

    try:
        # 2. Fetch all nodes
        nodes_resp = _post(url, _envelope(
            "<man:getAllNodes>"
            f"<man:sessionId>{session_id}</man:sessionId>"
            "</man:getAllNodes>"
        ), verify_tls)

        ns = {"man": _SOAP_NS}
        # DCE may wrap nodes as <return>, <nodes>, or <node> elements
        nodes = (
            nodes_resp.findall(".//man:return", ns)
            or nodes_resp.findall(".//return")
            or nodes_resp.findall(".//man:nodes", ns)
            or nodes_resp.findall(".//nodes")
        )

        devices: list[DceDevice] = []
        for node in nodes:
            ip = _child_text(node, "ipAddress", "ip")
            if not ip:
                continue
            devices.append(DceDevice(
                ip=ip,
                hostname=_child_text(node, "label", "name", "sysName"),
                location=_child_text(node, "location"),
                sysobjid=_child_text(node, "typeSystemOID", "sysObjectId", "sysObjectID"),
                node_id=_child_text(node, "nodeId", "id"),
            ))

        log.info("%s: DCE returned %d device(s)", host, len(devices))
        return devices

    finally:
        # 3. Close session (best-effort)
        try:
            _post(url, _envelope(
                "<man:closeSession>"
                f"<man:sessionId>{session_id}</man:sessionId>"
                "</man:closeSession>"
            ), verify_tls)
        except Exception:
            pass


# ── Public async API ──────────────────────────────────────────────────────────

async def fetch_devices(
    host: str,
    username: str,
    password: str,
    verify_tls: bool = False,
) -> list[DceDevice]:
    """Async wrapper — runs blocking SOAP calls in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _fetch_sync, host, username, password, verify_tls
    )
