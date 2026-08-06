from datetime import datetime, timezone

import fakeredis
import pytest

from src.common.config import Settings
from src.feature_engineering.feature_store import RedisFeatureStore
from src.feature_engineering.features import FeatureVector
from src.storage.redis_client import RedisClient

pytestmark = pytest.mark.unit


@pytest.fixture
def feature_store(monkeypatch):
    """RedisFeatureStore backed by fakeredis instead of a real server."""
    fake_conn = fakeredis.FakeRedis(decode_responses=True)

    client = RedisClient.__new__(RedisClient)
    client.settings = Settings()
    client.conn = fake_conn

    return RedisFeatureStore(settings=Settings(), client=client)


def _sample_feature_vector(card_id: str = "card_1") -> FeatureVector:
    return FeatureVector(
        card_id=card_id,
        as_of=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        txn_count_1h=1,
        txn_count_24h=1,
        avg_amount_1h=50.0,
        avg_amount_24h=50.0,
        sum_amount_1h=50.0,
        sum_amount_24h=50.0,
        distinct_merchants_24h=1,
        velocity_5min=1,
        seconds_since_last_txn=None,
        distance_from_last_txn_km=None,
        implied_speed_kmh=None,
        amount_zscore_24h=0.0,
        is_new_device=False,
    )


class TestRedisFeatureStore:
    def test_write_then_read_features_round_trips(self, feature_store):
        fv = _sample_feature_vector()
        feature_store.write_features(fv)

        result = feature_store.get_features("card_1")

        assert result is not None
        assert result["card_id"] == "card_1"
        assert result["avg_amount_1h"] == 50.0

    def test_missing_card_returns_none(self, feature_store):
        assert feature_store.get_features("card_does_not_exist") is None

    def test_append_history_and_read_back(self, feature_store, make_transaction):
        txn = make_transaction(card_id="card_1")
        feature_store.append_history(txn)

        history = feature_store.get_history("card_1")

        assert len(history) == 1
        assert history[0].transaction_id == txn.transaction_id

    def test_history_is_trimmed_to_max_length(self, feature_store, make_transaction):
        for i in range(600):
            feature_store.append_history(make_transaction(card_id="card_1", transaction_id=f"txn_{i}"))

        history = feature_store.get_history("card_1", limit=1000)

        assert len(history) == 500  # bounded by _MAX_HISTORY_LEN

    def test_get_session_state_combines_features_and_history(self, feature_store, make_transaction):
        feature_store.write_features(_sample_feature_vector())
        feature_store.append_history(make_transaction(card_id="card_1"))

        state = feature_store.get_session_state("card_1")

        assert state["features"]["card_id"] == "card_1"
        assert len(state["history"]) == 1
