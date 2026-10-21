"""Database-backed leases: at most one worker processes a given key at a time.

A lease expires on its own, so a crashed worker cannot block others for longer
than the TTL. Leases only prevent duplicate work; correctness does not depend on
them because cursor updates are compare-and-set (see ``ingest_range``).
"""

from __future__ import annotations

from tortoise import connections

_ACQUIRE_SQL = """
INSERT INTO worker_lease (name, owner, expires_at, updated_at)
VALUES ($1, $2, now() + make_interval(secs => $3), now())
ON CONFLICT (name) DO UPDATE
    SET owner = EXCLUDED.owner, expires_at = EXCLUDED.expires_at, updated_at = now()
    WHERE worker_lease.owner = EXCLUDED.owner OR worker_lease.expires_at < now()
RETURNING id
"""

_RELEASE_SQL = "UPDATE worker_lease SET expires_at = now() WHERE name = $1 AND owner = $2"


async def acquire_lease(name: str, owner: str, ttl_seconds: float) -> bool:
    """Acquire or renew ``name`` for ``owner``. False if someone else holds it."""
    # execute_query() would discard RETURNING rows of an INSERT; fetch them.
    rows = await connections.get("default").execute_query_dict(
        _ACQUIRE_SQL, [name, owner, float(ttl_seconds)]
    )
    return bool(rows)


async def release_lease(name: str, owner: str) -> None:
    await connections.get("default").execute_query(_RELEASE_SQL, [name, owner])
