"""
Data Center Expert (DCE) SOAP client.

Connects to a Schneider Electric Data Center Expert server via its web
services SOAP API and returns the list of monitored devices.

Service:  http[s]://<host>/integration/services/ISXCentralDeviceService_v2_0
WSDL:     http[s]://<host>/integration/services/ISXCentralDeviceService_v2_0?wsdl

Authentication: HTTP Basic Auth sent with every request. No sessions.

The four DCE web service endpoints are:
  ISXCentralDeviceService_v2_0      — device inventory (used here)
  ISXCentralSensorService_v2_0      — sensor readings
  ISXCentralAlarmsService_v2_0      — active/historical alarms
  ISXCentralDeviceGroupService_v2_0 — device group organisation
"""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# ── DCE SOAP API constants ─────────────────────────────────────────────────────

_DEVICE_SERVICE_PATH = "integration/services/ISXCentralDeviceService_v2_0"

# Namespace for request elements (getAllDevicesRequest, etc.)
_DEVICE_NS = "http://www.apc.com/stdws/xsd/ISXCentralDevices-v2"

# Namespace for response data types: ISXCDevice, ISXCNamedElement, ISXCElement, etc.
_COMMON_NS = "http://www.apc.com/stdws/xsd/ISXCentral/2009/10"

# SOAPAction required by SOAP 1.1 document/literal binding (from WSDL binding section)
_SOAP_ACTION = "http://www.apc.com/stdws/wsdl/ISXCentralDevices-v2/getAllDevices"

_NS_DECL = (
    'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
    f'xmlns:isx="{_DEVICE_NS}"'
)


# ── Public types ───────────────────────────────────────────────────────────────

@dataclass
class DceDevice:
    ip: str
    hostname: Optional[str]
    location: Optional[str]
    model_name: Optional[str]
    device_type: Optional[str]   # ISXCDeviceType from DCE
    comm_state: Optional[str]    # "ONLINE", "OFFLINE", etc.
    device_id: Optional[str]     # DCE-internal ISXCElementID
    name: Optional[str]          # human label in DCE (ISXCNamedElement/name)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _base_url(host: str) -> str:
    """Normalise host to a base URL. Defaults to https:// if no scheme given."""
    if host.startswith(("http://", "https://")):
        return host.rstrip("/")
    return f"https://{host}"


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def _ssl_ctx(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _envelope(body: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope {_NS_DECL}>'
        f"<soapenv:Header/>"
        f"<soapenv:Body>{body}</soapenv:Body>"
        f"</soapenv:Envelope>"
    ).encode("utf-8")


def _post(url: str, body: bytes, auth: str, verify: bool) -> ET.Element:
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": auth,
            "SOAPAction": _SOAP_ACTION,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=_ssl_ctx(verify), timeout=30) as resp:
        return ET.fromstring(resp.read())


def _text(element: ET.Element, *path: str) -> Optional[str]:
    """
    Walk a path of tag names (in _COMMON_NS) from element, return stripped text
    of the final node, or None if any step is missing or the text is empty.
    """
    cur = element
    for tag in path:
        cur = cur.find(f"{{{_COMMON_NS}}}{tag}")
        if cur is None:
            return None
    return (cur.text or "").strip() or None


# ── SOAP call ──────────────────────────────────────────────────────────────────

def _fetch_sync(host: str, username: str, password: str, verify_tls: bool) -> list[DceDevice]:
    """
    Blocking: POST getAllDevicesRequest to the DCE Device Service and parse
    the ISXCDevice elements from the response.
    """
    url = f"{_base_url(host)}/{_DEVICE_SERVICE_PATH}"
    auth = _basic_auth(username, password)

    body = _envelope(
        "<isx:getAllDevicesRequest>"
        "<isx:locale>en_US</isx:locale>"
        "</isx:getAllDevicesRequest>"
    )

    try:
        resp = _post(url, body, auth, verify_tls)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Data Center Expert at {host} returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to reach Data Center Expert at {host}: {exc}") from exc

    # ISXCDevice elements are in _COMMON_NS (data types namespace, not request namespace)
    device_els = resp.findall(f".//{{{_COMMON_NS}}}ISXCDevice")

    devices: list[DceDevice] = []
    for el in device_els:
        ip = _text(el, "ipAddress")
        if not ip:
            continue

        # name and id are nested: ISXCNamedElement/name and ISXCNamedElement/ISXCElement/id
        name = _text(el, "ISXCNamedElement", "name")
        device_id = _text(el, "ISXCNamedElement", "ISXCElement", "id")

        devices.append(DceDevice(
            ip=ip,
            hostname=_text(el, "hostName"),
            location=_text(el, "location"),
            model_name=_text(el, "modelName"),
            device_type=_text(el, "ISXCDeviceType"),
            comm_state=_text(el, "ISXCCommState"),
            device_id=device_id,
            name=name,
        ))

    log.info("Data Center Expert %s: %d device(s) returned", host, len(devices))
    return devices


# ── Public async API ───────────────────────────────────────────────────────────

async def fetch_devices(
    host: str,
    username: str,
    password: str,
    verify_tls: bool = False,
) -> list[DceDevice]:
    """Async wrapper — runs the blocking SOAP call in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _fetch_sync, host, username, password, verify_tls
    )
