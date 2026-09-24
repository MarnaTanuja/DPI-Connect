"""
Database setup — MongoDB.

Swapped from the original SQLite/SQLAlchemy MVP setup. Connects to
whatever `MONGO_URI` points at (a local mongod, Atlas, etc.) and hands
out a `pymongo.database.Database` handle. Mongo is schemaless, so there's
no `create_all()` step — `init_db()` just makes sure the indexes that
keep lookups fast/unique exist; collections themselves are created
lazily on first insert.

For tests (see backend/tests/test_workflow.py) set
`DPI_CONNECT_MONGO_MOCK=1` before importing the app and it uses an
in-memory `mongomock` client instead of a real connection — no live
MongoDB needed to run the test suite.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "dpi_connect")

_client = None
_db = None


def get_client():
    global _client
    if _client is None:
        if os.environ.get("DPI_CONNECT_MONGO_MOCK") == "1":
            import mongomock

            _client = mongomock.MongoClient()
        else:
            if not MONGO_URI:
                raise RuntimeError(
                    "MONGO_URI is not set. Copy backend/.env.example to "
                    "backend/.env and fill in MONGO_URI."
                )
            from pymongo import MongoClient

            _client = MongoClient(MONGO_URI)
    return _client


def get_database():
    global _db
    if _db is None:
        _db = get_client()[MONGO_DB_NAME]
    return _db


def get_db():
    """FastAPI dependency — yields the Mongo database handle."""
    return get_database()


def init_db() -> None:
    db = get_database()
    db.organizations.create_index("name", unique=True)
    db.organizations.create_index("api_key", unique=True)
    db.datasets.create_index("org_id")
    db.connection_requests.create_index("requester_org_id")
    db.connection_requests.create_index("counterparty_org_id")
    db.consents.create_index("connection_id", unique=True)
    db.audit_log.create_index("connection_id")


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
