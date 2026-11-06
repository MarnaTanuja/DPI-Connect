"""
X-Road secure-exchange adapter.

There is no real X-Road Security Server wired into this MVP — standing
one up (Security Server install, certs, subsystem registration with a
Central Server) is real infrastructure, not something to fake convincingly
in a demo. What this module DOES do is model the security *properties*
X-Road provides, so the rest of the app already talks to that contract and
swapping in the real thing later only means editing this one file:

  - mutual identification of both parties to an exchange
  - message-level signing (integrity + non-repudiation) of every payload
    that crosses an org boundary
  - a durable, signed audit trail of every exchange (X-Road's security
    servers log every request/response for this reason)

HOW TO REPLACE WITH REAL X-ROAD:
  - `secure_handshake()` -> instead of minting a local HMAC session token,
    call the Security Server's REST management API to verify both
    subsystems are registered and the service ACL permits the exchange.
  - `sign_payload()` / `verify_payload()` -> the Security Server signs and
    verifies messages for you (via SOAP/REST proxy + your org's cert) —
    you'd stop doing this in application code entirely and instead route
    the actual HTTP call for /exchange through your local Security Server,
    which handles TLS, signing, and logging transparently.
  - Keep `AuditLog` — X-Road's own logs are the source of truth once
    integrated, but mirroring key events in your own DB is still good
    practice for application-level visibility.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.request
import uuid
from datetime import datetime, timezone

# In production this must be a securely-provisioned secret (or, once real
# X-Road is integrated, this whole HMAC scheme goes away in favor of the
# Security Server's own cert-based signing). Never commit a real secret.
_SHARED_SECRET = os.environ.get("XROAD_SHARED_SECRET", "dev-only-insecure-secret-change-me")


# ---- Real X-Road Security Server integration (REST) -------------------------
# Enabled when XROAD_SECURITY_SERVER_URL is set. Otherwise the original local
# HMAC simulation is used, so nothing changes for existing setups.
#   XROAD_SECURITY_SERVER_URL  e.g. http://my-ss:8080   (your local Security Server)
#   XROAD_INSTANCE / XROAD_MEMBER_CLASS / XROAD_MEMBER_CODE  your member identity
#   XROAD_SUBSYSTEM_PREFIX     subsystem code = prefix + org_id (default "org-")
#   XROAD_SERVICE              REST service code on the counterparty (default "dpi-exchange")
#   XROAD_STRICT=1             fail instead of falling back to simulation
_SS_URL = os.environ.get("XROAD_SECURITY_SERVER_URL", "").rstrip("/")
_INSTANCE = os.environ.get("XROAD_INSTANCE", "DEV")
_MCLASS = os.environ.get("XROAD_MEMBER_CLASS", "GOV")
_MCODE = os.environ.get("XROAD_MEMBER_CODE", "DPI")
_PREFIX = os.environ.get("XROAD_SUBSYSTEM_PREFIX", "org-")
_SERVICE = os.environ.get("XROAD_SERVICE", "dpi-exchange")
_STRICT = os.environ.get("XROAD_STRICT", "0") == "1"


def real_enabled() -> bool:
    return bool(_SS_URL)


def _sub(org_id: str) -> str:
    return f"{_PREFIX}{org_id}"


def _client_header(org_id: str) -> str:
    return f"{_INSTANCE}/{_MCLASS}/{_MCODE}/{_sub(org_id)}"


def _ss_request(method: str, path: str, client_org: str, body: dict | None = None) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _SS_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "X-Road-Client": _client_header(client_org),
                 "X-Road-Id": str(uuid.uuid4())},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode() or "{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"raw": raw[:500]}
        return r.status, dict(r.headers), parsed


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, default=str).encode("utf-8")


def sign_payload(payload: dict) -> str:
    """HMAC-SHA256 signature standing in for X-Road message signing."""
    return hmac.new(_SHARED_SECRET.encode(), _canonical(payload), hashlib.sha256).hexdigest()


def verify_payload(payload: dict, signature: str) -> bool:
    expected = sign_payload(payload)
    return hmac.compare_digest(expected, signature)


def secure_handshake(requester_org_id: str, counterparty_org_id: str, connection_id: str) -> dict:
    """
    Simulates establishing a secure channel between two subsystems, the
    way a real X-Road Security Server negotiates before allowing a service
    call between two registered subsystems. Returns a signed handshake
    record suitable for storing in AuditLog.detail_json.
    """
    handshake = {
        "event": "xroad_secure_handshake",
        "requester_org_id": requester_org_id,
        "counterparty_org_id": counterparty_org_id,
        "connection_id": connection_id,
        "protocol": "X-Road (simulated)",
        "established_at": datetime.now(timezone.utc).isoformat(),
    }
    if real_enabled():
        try:
            # Ask the Security Server (listClients metaservice) whether both
            # subsystems are registered in this X-Road instance.
            _, _, clients = _ss_request("GET", f"/r1/{_INSTANCE}/listClients", requester_org_id)
            known = {c.get("id", {}).get("subsystem_code") for c in clients.get("member", [])}
            ok = "*" in known or (_sub(requester_org_id) in known and _sub(counterparty_org_id) in known)
            handshake.update(protocol="X-Road (Security Server)", security_server=_SS_URL,
                             both_subsystems_registered=ok)
            if not ok and _STRICT:
                raise RuntimeError("Subsystem not registered in X-Road")
        except Exception as e:  # noqa: BLE001
            if _STRICT:
                raise
            handshake.update(protocol="X-Road (simulated fallback)", xroad_error=str(e)[:200])
    handshake["signature"] = sign_payload(handshake)
    return handshake


def secure_exchange_envelope(connection_id: str, payload: dict, requester_org_id: str | None = None, counterparty_org_id: str | None = None) -> dict:
    """
    Wraps an actual data payload the way it would cross an X-Road channel:
    signed, timestamped, traceable back to the connection that authorized it.
    """
    envelope = {
        "event": "xroad_secure_exchange",
        "connection_id": connection_id,
        "payload_hash": hashlib.sha256(_canonical(payload)).hexdigest(),
        "record_count": len(payload.get("records", [])),
        "exchanged_at": datetime.now(timezone.utc).isoformat(),
    }
    if real_enabled():
        try:
            requester = requester_org_id or connection_id
            counterparty = counterparty_org_id or connection_id
            status, headers, resp = _ss_request(
                "POST", f"/r1/{_INSTANCE}/{_MCLASS}/{_MCODE}/{_sub(counterparty)}/{_SERVICE}",
                requester, {k: v for k, v in envelope.items() if k != "signature"})
            envelope.update(protocol="X-Road (Security Server)", xroad_status=status,
                            xroad_message_id=headers.get("X-Road-Id"))
        except Exception as e:  # noqa: BLE001
            if _STRICT:
                raise
            envelope.update(protocol="X-Road (simulated fallback)", xroad_error=str(e)[:200])
    envelope["signature"] = sign_payload(envelope)
    return envelope
