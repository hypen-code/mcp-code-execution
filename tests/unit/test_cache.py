"""Unit tests for the code cache store."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mfp.runtime.cache import CacheStore


@pytest.fixture
async def cache(tmp_path: Path) -> CacheStore:
    store = CacheStore(
        db_path=str(tmp_path / "test_cache.db"),
        ttl_seconds=3600,
        max_entries=10,
    )
    await store.initialize()
    return store


async def test_store_and_retrieve(cache: CacheStore) -> None:
    """Stored entry can be retrieved by ID."""
    code = "result = 42"
    entry_id = await cache.store(code, "compute 42", ["weather"], "hash123")

    entry = await cache.get(entry_id)
    assert entry is not None
    assert entry.code == code
    assert entry.description == "compute 42"
    assert entry.servers_used == ["weather"]


async def test_get_nonexistent_returns_none(cache: CacheStore) -> None:
    """Getting a non-existent ID returns None."""
    result = await cache.get("nonexistent_id_12345")
    assert result is None


async def test_search_by_description(cache: CacheStore) -> None:
    """Search returns entries matching description substring."""
    await cache.store("result = 1", "get weather data", ["weather"], "hash1")
    await cache.store("result = 2", "list hotels", ["hotel"], "hash2")
    await cache.store("result = 3", "book hotel room", ["hotel"], "hash3")

    results = await cache.search("hotel")
    assert len(results) == 2
    descriptions = {e.description for e in results}
    assert "list hotels" in descriptions
    assert "book hotel room" in descriptions


async def test_search_no_filter_returns_all(cache: CacheStore) -> None:
    """Search without filter returns all valid entries."""
    await cache.store("result = 1", "entry one", [], "hash1")
    await cache.store("result = 2", "entry two", [], "hash2")

    results = await cache.search()
    assert len(results) == 2


async def test_use_count_increments_on_duplicate(cache: CacheStore) -> None:
    """Storing same code increments use_count."""
    code = "result = 99"
    await cache.store(code, "test code", [], "hash1")
    await cache.store(code, "test code again", [], "hash1")

    # Same code -> same ID -> use_count should be 2
    import hashlib  # noqa: PLC0415
    normalized = "\n".join(line.rstrip() for line in code.splitlines() if line.strip())
    entry_id = hashlib.sha256(normalized.encode()).hexdigest()

    entry = await cache.get(entry_id)
    assert entry is not None
    assert entry.use_count >= 2


async def test_expired_entry_not_returned(tmp_path: Path) -> None:
    """Entries past TTL are removed and not returned."""
    cache = CacheStore(
        db_path=str(tmp_path / "ttl_cache.db"),
        ttl_seconds=1,  # 1 second TTL
        max_entries=10,
    )
    await cache.initialize()

    code = "result = 'expired'"
    entry_id = await cache.store(code, "short lived", [], "hash1")

    # Wait for TTL to expire
    time.sleep(1.1)

    entry = await cache.get(entry_id)
    assert entry is None


async def test_cleanup_expired_removes_old_entries(tmp_path: Path) -> None:
    """cleanup_expired removes entries past their TTL."""
    cache = CacheStore(
        db_path=str(tmp_path / "cleanup_cache.db"),
        ttl_seconds=1,
        max_entries=50,
    )
    await cache.initialize()

    await cache.store("result = 1", "old entry", [], "hash1")
    time.sleep(1.1)

    removed = await cache.cleanup_expired()
    assert removed >= 1


async def test_lru_eviction_on_max_entries(tmp_path: Path) -> None:
    """Cache evicts LRU entries when max_entries is exceeded."""
    cache = CacheStore(
        db_path=str(tmp_path / "lru_cache.db"),
        ttl_seconds=3600,
        max_entries=3,
    )
    await cache.initialize()

    for i in range(5):
        await cache.store(f"result = {i}", f"code {i}", [], f"hash{i}")

    results = await cache.search()
    assert len(results) <= 3


async def test_invalidate_by_swagger_hash(cache: CacheStore) -> None:
    """invalidate_by_swagger_hash removes entries with matching hash."""
    await cache.store("result = 1", "uses old api", ["weather"], "old_hash_abc")
    await cache.store("result = 2", "uses new api", ["weather"], "new_hash_xyz")

    removed = await cache.invalidate_by_swagger_hash("old_hash_abc")
    assert removed == 1

    results = await cache.search()
    assert all(e.description != "uses old api" for e in results)
