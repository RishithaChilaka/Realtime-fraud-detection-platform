"""
Custom Redis-backed feature store.

Two responsibilities, deliberately kept in one small class because they
share the same storage engine and latency budget:

1. Serving the latest precomputed `FeatureVector` per card for real-time
   scoring (<10ms p95 target -- a single Redis GET on a small hash).
2. Maintaining a bounded rolling history of recent transactions per card
   (a Redis list, trimmed to a max length/age) so `features.compute_features`
   has the input it needs without re-scanning PostgreSQL on every event.

A Feast-based store is a natural drop-in replacement later (this class
implements the minimal `get_features`/`write_features` surface Feast's
online store also exposes), but for Phase 1 a direct Redis client keeps
the dependency footprint and operational surface small.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Optional

from src.common.config import Settings, get_settings
from src.common.schemas import Transaction
from src.feature_engineering.features import FeatureVector
from src.monitoring.metrics import FEATURE_STORE_READ_LATENCY, FEATURE_STORE_WRITE_LATENCY
from src.storage.redis_client import RedisClient

_FEATURE_KEY_PREFIX = "features:card:"
_HISTORY_KEY_PREFIX = "history:card:"
_MAX_HISTORY_LEN = 500  # bounded so a single hot card can't grow memory unboundedly


class RedisFeatureStore:
    def __init__(
        self, settings: Optional[Settings] = None, client: Optional[RedisClient] = None
    ) -> None:
        self.settings = settings or get_settings()
        self._redis = (client or RedisClient(self.settings)).conn

    @staticmethod
    def _feature_key(card_id: str) -> str:
        return f"{_FEATURE_KEY_PREFIX}{card_id}"

    @staticmethod
    def _history_key(card_id: str) -> str:
        return f"{_HISTORY_KEY_PREFIX}{card_id}"

    def write_features(self, feature_vector: FeatureVector) -> None:
        start = time.perf_counter()
        try:
            self._redis.set(
                self._feature_key(feature_vector.card_id),
                json.dumps(feature_vector.to_dict()),
                ex=self.settings.feature_ttl_seconds,
            )
        finally:
            FEATURE_STORE_WRITE_LATENCY.observe(time.perf_counter() - start)

    def get_features(self, card_id: str) -> Optional[dict]:
        start = time.perf_counter()
        try:
            raw = self._redis.get(self._feature_key(card_id))
            return json.loads(raw) if raw else None
        finally:
            FEATURE_STORE_READ_LATENCY.observe(time.perf_counter() - start)

    def append_history(self, txn: Transaction) -> None:
        """Push the transaction onto the card's history list and trim it to
        a bounded length + TTL so history never grows unbounded for hot
        cards or stale ones."""
        key = self._history_key(txn.card_id)
        pipe = self._redis.pipeline()
        pipe.lpush(key, txn.model_dump_json())
        pipe.ltrim(key, 0, _MAX_HISTORY_LEN - 1)
        pipe.expire(key, self.settings.feature_ttl_seconds)
        pipe.execute()

    def get_history(self, card_id: str, limit: int = _MAX_HISTORY_LEN) -> list[Transaction]:
        key = self._history_key(card_id)
        raw_items = self._redis.lrange(key, 0, limit - 1)
        return [Transaction.model_validate_json(item) for item in raw_items]

    def get_session_state(self, card_id: str) -> dict:
        """Convenience combined read used by low-latency scoring callers
        that want both the latest features and the raw recent history in
        one round trip's worth of application logic (two Redis calls, but
        both sub-millisecond and pipeline-able by the caller if needed)."""
        return {
            "features": self.get_features(card_id),
            "history": [t.model_dump(mode="json") for t in self.get_history(card_id, limit=20)],
            "as_of": datetime.utcnow().isoformat(),
        }
