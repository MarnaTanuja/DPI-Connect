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
from datetime import datetime, timezone

# In production this must be a securely-provisioned secret (or, once real
# X-Road is integrated, this whole HMAC scheme goes away in favor of the
# Security Server's own cert-based signing). Never commit a real secret.
_SHARED_SECRET = os.environ.get("XROAD_SHARED_SECRET", "dev-only-insecure-secret-change-me")


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
    handshake["signature"] = sign_payload(handshake)
    return handshake


def secure_exchange_envelope(connection_id: str, payload: dict) -> dict:
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
    envelope["signature"] = sign_payload(envelope)
    return envelope
