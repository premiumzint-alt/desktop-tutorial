"""Time-based retention for the MongoDB collections.

MongoDB deletes expired documents itself through a TTL index, so no background
worker is needed. An existing plain ``timestamp`` index cannot be turned into a TTL
index in place, so it is dropped and recreated with ``expireAfterSeconds``.

The values live in SQLite (``retention_settings``) so the Settings panel can change
them, seeded from ``.env`` the first time; the environment remains the default.
"""

from typing import Any

from sqlalchemy.orm import Session

from .config import settings
from .models import RetentionSetting

SECONDS_PER_DAY = 24 * 60 * 60

RETENTION_FIELDS: dict[str, str] = {
    "metrics": "metrics_days",
    "security_events": "security_events_days",
    "scan_results": "scan_results_days",
}

DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "metrics": settings.metrics_retention_days,
    "security_events": settings.security_events_retention_days,
    "scan_results": settings.scan_results_retention_days,
}


def retention_record(db: Session) -> RetentionSetting:
    """Returns the single settings row, creating it from the defaults once."""
    record = db.get(RetentionSetting, 1)
    if record is None:
        record = RetentionSetting(
            id=1,
            **{field: DEFAULT_RETENTION_DAYS[name] for name, field in RETENTION_FIELDS.items()},
        )
        db.add(record)
        db.commit()
        db.refresh(record)
    return record


def retention_days(db: Session) -> dict[str, int]:
    record = retention_record(db)
    return {name: getattr(record, field) for name, field in RETENTION_FIELDS.items()}


def ensure_ttl_index(collection: Any, days: int) -> str:
    """Makes the ``timestamp`` index a TTL index and reports what changed.

    ``days <= 0`` keeps documents forever, which is the behaviour before retention
    existed.
    """
    if days <= 0:
        return "disabled"
    seconds = days * SECONDS_PER_DAY
    for index in collection.list_indexes():
        if dict(index.get("key") or {}) != {"timestamp": 1}:
            continue
        if index.get("expireAfterSeconds") == seconds:
            return "unchanged"
        collection.drop_index(index["name"])
        break
    collection.create_index("timestamp", expireAfterSeconds=seconds)
    return "created"


def apply_retention(database: Any, days: dict[str, int] | None = None) -> dict[str, str]:
    """Applies retention to every monitored collection."""
    values = days or DEFAULT_RETENTION_DAYS
    results: dict[str, str] = {}
    for name in RETENTION_FIELDS:
        results[name] = ensure_ttl_index(database[name], values.get(name, DEFAULT_RETENTION_DAYS[name]))
    return results
