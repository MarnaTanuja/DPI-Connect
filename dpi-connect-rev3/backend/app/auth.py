"""
Simple API-key auth with two kinds of principal:

  * an Organization  - one key per org, issued at registration (shown once)
  * the platform ADMIN - one key from the ADMIN_API_KEY environment variable

Every authenticated endpoint expects the key in the `X-API-Key` header.
This is deliberately minimal - no passwords, no JWT refresh, no roles within
an org - fine for a pilot with a handful of known organizations, not for
exposing on the open internet as-is. Swap for OAuth2 / per-user accounts
(with a real admin role) before going further than a pilot.
"""

from __future__ import annotations

import hmac
import os
import warnings
from typing import Any

from fastapi import Depends, Header, HTTPException

from app.database import get_db
from app.models_db import ORGANIZATIONS

_DEV_ADMIN_KEY = "admin-dev-key-change-me"


def admin_key() -> str:
    key = os.environ.get("ADMIN_API_KEY")
    if not key:
        warnings.warn(
            "ADMIN_API_KEY is not set - using the insecure development key "
            f"'{_DEV_ADMIN_KEY}'. Set ADMIN_API_KEY in .env before any real use.",
            stacklevel=2,
        )
        return _DEV_ADMIN_KEY
    return key


def is_admin_key(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode(), admin_key().encode())


def get_current_org(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db=Depends(get_db),
) -> dict[str, Any]:
    org = db[ORGANIZATIONS].find_one({"api_key": x_api_key})
    if not org:
        raise HTTPException(401, "invalid API key")
    return org


def get_current_admin(x_api_key: str = Header(..., alias="X-API-Key"), db=Depends(get_db)) -> dict[str, Any]:
    if is_admin_key(x_api_key):
        return {"_id": "admin", "name": "Platform admin", "role": "admin"}
    if db[ORGANIZATIONS].find_one({"api_key": x_api_key}):
        raise HTTPException(403, "this endpoint is for the platform admin only")
    raise HTTPException(401, "invalid API key")
