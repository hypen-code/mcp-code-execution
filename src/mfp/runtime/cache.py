"""SQLite-backed code cache with TTL and LRU eviction."""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from mfp.errors import CacheError
from mfp.models import CacheEntry, CacheSummary
from mfp.utils.hashing import hash_code
from mfp.utils.logging import get_logger

logger = get_logger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS code_cache (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    code TEXT NOT NULL,
    servers_used TEXT NOT NULL,
    swagger_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    use_count INTEGER DEFAULT 1,
    ttl_seconds INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_last_used ON code_cache(last_used_at);
CREATE INDEX IF NOT EXISTS idx_cache_description ON code_cache(description);
"""


class CacheStore:
    """Async SQLite-backed cache for successfully executed code snippets."""

    def __init__(self, db_path: str, ttl_seconds: int = 3600, max_entries: int = 500) -> None:
        """Initialize the cache store.

        Args:
            db_path: Filesystem path for the SQLite database file.
            ttl_seconds: Default cache entry lifetime in seconds.
            max_entries: Maximum number of entries before LRU eviction.
        """
        self._db_path = db_path
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries

    async def initialize(self) -> None:
        """Create database tables if they don't exist.

        Raises:
            CacheError: If database initialization fails.
        """
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.executescript(_CREATE_TABLE_SQL)
                await db.commit()
            logger.info("cache_initialized", path=self._db_path)
        except aiosqlite.Error as exc:
            raise CacheError(f"Failed to initialize cache database: {exc}") from exc

    async def store(
        self,
        code: str,
        description: str,
        servers_used: list[str],
        swagger_hash: str,
    ) -> str:
        """Store a successfully executed code snippet in the cache.

        Args:
            code: Python source code that was executed.
            description: Brief description of what the code does.
            servers_used: Names of API servers the code uses.
            swagger_hash: Combined hash of swagger specs used.

        Returns:
            Cache entry ID (SHA256 of code).

        Raises:
            CacheError: If storage fails.
        """
        entry_id = hash_code(code)
        now = time.time()

        try:
            async with aiosqlite.connect(self._db_path) as db:
                # Upsert — increment use_count if already exists
                await db.execute(
                    """
                    INSERT INTO code_cache
                        (id, description, code, servers_used, swagger_hash, created_at, last_used_at, use_count, ttl_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        last_used_at = excluded.last_used_at,
                        use_count = use_count + 1
                    """,
                    (
                        entry_id,
                        description,
                        code,
                        json.dumps(servers_used),
                        swagger_hash,
                        now,
                        now,
                        self._ttl_seconds,
                    ),
                )
                await db.commit()

            logger.debug("cache_stored", id=entry_id[:12], description=description[:50])
            await self._evict_if_needed()
            return entry_id

        except aiosqlite.Error as exc:
            raise CacheError(f"Failed to store cache entry: {exc}") from exc

    async def get(self, entry_id: str) -> CacheEntry | None:
        """Retrieve a cache entry by ID.

        Updates last_used_at on access and checks TTL validity.

        Args:
            entry_id: Cache entry ID (SHA256 hash).

        Returns:
            CacheEntry if found and valid, None otherwise.

        Raises:
            CacheError: If database access fails.
        """
        now = time.time()

        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM code_cache WHERE id = ?", (entry_id,)
                ) as cursor:
                    row = await cursor.fetchone()

                if row is None:
                    logger.debug("cache_miss", id=entry_id[:12])
                    return None

                entry = self._row_to_entry(row)

                # Check TTL
                if now - entry.created_at > entry.ttl_seconds:
                    await self._delete(db, entry_id)
                    await db.commit()
                    logger.debug("cache_expired", id=entry_id[:12])
                    return None

                # Update last_used_at
                await db.execute(
                    "UPDATE code_cache SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                    (now, entry_id),
                )
                await db.commit()

            logger.debug("cache_hit", id=entry_id[:12])
            return entry

        except aiosqlite.Error as exc:
            raise CacheError(f"Failed to retrieve cache entry: {exc}") from exc

    async def search(self, query: str | None = None, limit: int = 50) -> list[CacheSummary]:
        """Search cache entries by description.

        Args:
            query: Optional search term to filter by description (case-insensitive).
            limit: Maximum entries to return.

        Returns:
            List of CacheSummary objects ordered by use count descending.

        Raises:
            CacheError: If database access fails.
        """
        now = time.time()

        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row

                if query:
                    sql = """
                        SELECT id, description, servers_used, use_count, created_at, ttl_seconds
                        FROM code_cache
                        WHERE description LIKE ? AND (? - created_at) < ttl_seconds
                        ORDER BY use_count DESC, last_used_at DESC
                        LIMIT ?
                    """
                    params = (f"%{query}%", now, limit)
                else:
                    sql = """
                        SELECT id, description, servers_used, use_count, created_at, ttl_seconds
                        FROM code_cache
                        WHERE (? - created_at) < ttl_seconds
                        ORDER BY use_count DESC, last_used_at DESC
                        LIMIT ?
                    """
                    params = (now, limit)  # type: ignore[assignment]

                async with db.execute(sql, params) as cursor:
                    rows = await cursor.fetchall()

            return [
                CacheSummary(
                    id=row["id"],
                    description=row["description"],
                    servers_used=json.loads(row["servers_used"]),
                    use_count=row["use_count"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

        except aiosqlite.Error as exc:
            raise CacheError(f"Failed to search cache: {exc}") from exc

    async def invalidate_by_swagger_hash(self, swagger_hash: str) -> int:
        """Remove all cache entries with a stale swagger hash.

        Args:
            swagger_hash: Old hash to invalidate.

        Returns:
            Number of entries removed.

        Raises:
            CacheError: If database access fails.
        """
        try:
            async with aiosqlite.connect(self._db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM code_cache WHERE swagger_hash = ?", (swagger_hash,)
                )
                await db.commit()
                count = cursor.rowcount

            if count:
                logger.info("cache_invalidated", swagger_hash=swagger_hash[:12], count=count)
            return count

        except aiosqlite.Error as exc:
            raise CacheError(f"Failed to invalidate cache entries: {exc}") from exc

    async def cleanup_expired(self) -> int:
        """Remove all expired cache entries.

        Returns:
            Number of entries removed.

        Raises:
            CacheError: If cleanup fails.
        """
        now = time.time()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM code_cache WHERE (? - created_at) >= ttl_seconds", (now,)
                )
                await db.commit()
                count = cursor.rowcount

            logger.debug("cache_expired_cleaned", count=count)
            return count

        except aiosqlite.Error as exc:
            raise CacheError(f"Failed to clean expired entries: {exc}") from exc

    async def _evict_if_needed(self) -> None:
        """Evict LRU entries if cache is over max_entries limit."""
        try:
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute("SELECT COUNT(*) FROM code_cache") as cursor:
                    row = await cursor.fetchone()
                    count = row[0] if row else 0

                if count > self._max_entries:
                    excess = count - self._max_entries
                    await db.execute(
                        """
                        DELETE FROM code_cache WHERE id IN (
                            SELECT id FROM code_cache
                            ORDER BY last_used_at ASC
                            LIMIT ?
                        )
                        """,
                        (excess,),
                    )
                    await db.commit()
                    logger.info("cache_evicted_lru", count=excess)

        except aiosqlite.Error as exc:
            logger.warning("cache_eviction_failed", error=str(exc))

    async def _delete(self, db: aiosqlite.Connection, entry_id: str) -> None:
        """Delete a single cache entry.

        Args:
            db: Active database connection.
            entry_id: Entry ID to delete.
        """
        await db.execute("DELETE FROM code_cache WHERE id = ?", (entry_id,))

    def _row_to_entry(self, row: aiosqlite.Row) -> CacheEntry:
        """Convert a database row to a CacheEntry model.

        Args:
            row: SQLite row object.

        Returns:
            CacheEntry Pydantic model.
        """
        return CacheEntry(
            id=row["id"],
            description=row["description"],
            code=row["code"],
            servers_used=json.loads(row["servers_used"]),
            swagger_hash=row["swagger_hash"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            use_count=row["use_count"],
            ttl_seconds=row["ttl_seconds"],
        )
