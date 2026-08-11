"""Thin, connection-pooled Redis client wrapper used by the feature store
and by any future session-state consumers."""

from __future__ import annotations

from typing import Optional

import redis
from redis.connection import SSLConnection

from src.common.config import Settings, get_settings


class RedisClient:
    _pool: Optional[redis.ConnectionPool] = None

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        if RedisClient._pool is None:
            # ConnectionPool forwards its kwargs straight to the connection
            # class's __init__ -- the plain Connection/AbstractConnection
            # used by default has no `ssl` parameter at all (that's not how
            # redis-py does TLS), so passing ssl=False there unconditionally
            # raised "unexpected keyword argument 'ssl'" even in the local/
            # no-TLS case. TLS instead means swapping in connection_class=
            # SSLConnection; the plain default is used otherwise.
            pool_kwargs = dict(
                host=self.settings.redis_host,
                port=self.settings.redis_port,
                db=self.settings.redis_db,
                # Local docker-compose Redis has no AUTH/TLS (empty token ->
                # password=None); ElastiCache in production requires both --
                # see terraform/modules/elasticache.
                password=self.settings.redis_auth_token or None,
                decode_responses=True,
                max_connections=50,
            )
            if self.settings.redis_use_tls:
                pool_kwargs["connection_class"] = SSLConnection
            RedisClient._pool = redis.ConnectionPool(**pool_kwargs)
        self.conn = redis.Redis(connection_pool=RedisClient._pool)

    def ping(self) -> bool:
        return bool(self.conn.ping())
