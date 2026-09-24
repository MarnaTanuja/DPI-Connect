"""
Simple API-key auth. Each Organization gets one key at registration time
(shown once). Every authenticated endpoint expects it in the `X-API-Key`
header. This is deliberately minimal — no passwords, no JWT refresh, no
roles-within-an-org — fine for a pilot with a handful of organizations,
not something to expose on the open internet as-is. Swap for OAuth2 /
per-user accounts within an org before going further than a pilot.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, HTTPException

from app.database import get_db
from app.models_db import ORGANIZATIONS


def get_current_org(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db=Depends(get_db),
) -> dict[str, Any]:
    org = db[ORGANIZATIONS].find_one({"api_key": x_api_key})
    if not org:
        raise HTTPException(401, "invalid API key")
    return org
