"""
utils/cache.py
──────────────
Redis-backed caching helpers for BandBoard.

Usage
─────
    from utils.cache import cache_get, cache_set, cache_delete, cached

    # Manual get/set
    data = cache_get("auditions:list")
    if data is None:
        data = expensive_db_query()
        cache_set("auditions:list", data, ttl=120)

    # Decorator
    @cached("index:auditions", ttl=60)
    def get_open_auditions():
        ...

Cache key conventions
─────────────────────
    auditions:list              – all open auditions (index + browse page)
    auditions:detail:<id>       – single audition detail
    instruments:list            – distinct instruments for filter bar
    genres:list                 – distinct genres for filter bar
    user:auditions:<user_id>    – band's own auditions list
"""

import json
import logging
from functools import wraps
from typing import Any, Callable, Optional

from extensions import get_redis

log = logging.getLogger(__name__)

# Default TTL values (seconds)
TTL_SHORT  = 30    # live data (counts, statuses)
TTL_MEDIUM = 120   # browse / index pages
TTL_LONG   = 600   # reference data (instruments, genres)


def _redis():
    """Return Redis client; returns None on connection error so the app degrades gracefully."""
    try:
        r = get_redis()
        r.ping()
        return r
    except Exception as exc:
        log.warning("Redis unavailable: %s – running without cache", exc)
        return None


def cache_get(key: str) -> Optional[Any]:
    """Return deserialised value for *key*, or None on miss / error."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:
        log.warning("cache_get(%s) failed: %s", key, exc)
        return None


def cache_set(key: str, value: Any, ttl: int = TTL_MEDIUM) -> bool:
    """Serialise *value* and store under *key* with *ttl* seconds expiry."""
    r = _redis()
    if r is None:
        return False
    try:
        r.setex(key, ttl, json.dumps(value, default=str))
        return True
    except Exception as exc:
        log.warning("cache_set(%s) failed: %s", key, exc)
        return False


def cache_delete(*keys: str) -> int:
    """Delete one or more keys. Returns number of keys deleted."""
    r = _redis()
    if r is None:
        return 0
    try:
        return r.delete(*keys)
    except Exception as exc:
        log.warning("cache_delete failed: %s", exc)
        return 0


def cache_delete_pattern(pattern: str) -> int:
    """Delete all keys matching a glob *pattern* (use sparingly – does SCAN)."""
    r = _redis()
    if r is None:
        return 0
    deleted = 0
    try:
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match=pattern, count=100)
            if keys:
                deleted += r.delete(*keys)
            if cursor == 0:
                break
    except Exception as exc:
        log.warning("cache_delete_pattern(%s) failed: %s", pattern, exc)
    return deleted


def cached(key: str, ttl: int = TTL_MEDIUM) -> Callable:
    """
    Function decorator.  Caches the return value of the wrapped function.

    The *key* may contain ``{arg}`` placeholders that are filled from the
    function's keyword arguments::

        @cached("auditions:detail:{aid}", ttl=TTL_SHORT)
        def get_audition(aid: int):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            resolved_key = key.format(**kwargs)
            hit = cache_get(resolved_key)
            if hit is not None:
                return hit
            result = fn(*args, **kwargs)
            if result is not None:
                cache_set(resolved_key, result, ttl)
            return result
        return wrapper
    return decorator


# ── Invalidation helpers ──────────────────────────────────────────────────────

def invalidate_audition_caches(audition_id: Optional[int] = None):
    """
    Call after any write that changes audition data.
    Invalidates the browse list, index list, and optionally a specific detail.
    """
    keys = ["auditions:list", "auditions:index", "instruments:list", "genres:list"]
    if audition_id:
        keys.append(f"auditions:detail:{audition_id}")
    cache_delete(*keys)


def invalidate_user_caches(user_id: int):
    """Call after a user's own auditions or applications change."""
    cache_delete(
        f"user:auditions:{user_id}",
        f"user:applications:{user_id}",
    )
