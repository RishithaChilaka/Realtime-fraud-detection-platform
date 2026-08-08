"""Thin, connection-pooled Redis client wrapper used by the feature store
and by any future session-state consumers."""
from __future__ import annotations

from typing import Optional

import redis

from src.common.config import Settings, get_settings


class RedisClient:
    _pool: Optional[redis.ConnectionPool] = None

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        if RedisClient._pool is None:
            RedisClient._pool = redis.ConnectionPool(
                host=self.settings.redis_host,
                port=self.settings.redis_port,
                db=self.settings.redis_db,
                # Local docker-compose Redis has no AUTH/TLS (empty
                # token -> `password=None`, `ssl=False`); ElastiCache in
                # production requires both -- see terraform/modules/elasticache.
                password=self.settings.redis_auth_token or None,
                ssl=self.settings.redis_use_tls,
                decode_responses=True,
                max_connections=50,
            )
        self.conn = redis.Redis(connection_pool=RedisClient._pool)

    def ping(self) -> bool:
        return bool(self.conn.ping())
