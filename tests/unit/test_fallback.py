from datetime import datetime, timedelta, timezone

import pytest

from src.api.fallback import RuleBasedFallback
from src.common.config import Settings
from src.feature_engineering.features import compute_features

pytestmark = pytest.mark.unit

BASE_TIME = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings():
    return Settings(
        fallback_velocity_5min_threshold=10,
        fallback_amount_zscore_threshold=5.0,
        fallback_impossible_travel_speed_kmh=900.0,
    )


@pytest.fixture
def fallback(settings):
    return RuleBasedFallback(settings)


class TestRuleBasedFallback:
    def test_normal_transaction_scores_low(self, fallback, make_transaction):
        current = make_transaction(event_time=BASE_TIME)
        fv = compute_features(history=[], current=current)

        result = fallback.score(fv)

        assert result.score < 0.5
        assert result.triggered_rules == []

    def test_high_velocity_triggers_flag(self, fallback, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(seconds=s))
            for s in range(10, 130, 10)
        ]
        current = make_transaction(event_time=BASE_TIME)
        fv = compute_features(history, current)

        result = fallback.score(fv)

        assert result.score >= 0.9
        assert any("velocity_5min" in r for r in result.triggered_rules)

    def test_amount_outlier_triggers_flag(self, fallback, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(hours=h), amount=amt)
            for h, amt in zip(range(1, 6), [50, 55, 45, 52, 48])
        ]
        current = make_transaction(event_time=BASE_TIME, amount=10000.0)
        fv = compute_features(history, current)

        result = fallback.score(fv)

        assert result.score >= 0.8
        assert any("amount_zscore_24h" in r for r in result.triggered_rules)

    def test_impossible_travel_triggers_flag(self, fallback, make_transaction):
        history = [
            make_transaction(
                event_time=BASE_TIME - timedelta(minutes=5), latitude=37.7749, longitude=-122.4194
            )
        ]
        current = make_transaction(event_time=BASE_TIME, latitude=40.7128, longitude=-74.0060)
        fv = compute_features(history, current)

        result = fallback.score(fv)

        assert result.score >= 0.9
        assert any("implied_speed_kmh" in r for r in result.triggered_rules)

    def test_reason_property_joins_triggered_rules(self, fallback, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(seconds=s))
            for s in range(10, 130, 10)
        ]
        current = make_transaction(event_time=BASE_TIME)
        fv = compute_features(history, current)

        result = fallback.score(fv)

        assert "velocity_5min" in result.reason

    def test_no_rules_triggered_reason_is_explicit(self, fallback, make_transaction):
        current = make_transaction(event_time=BASE_TIME)
        fv = compute_features(history=[], current=current)

        result = fallback.score(fv)

        assert result.reason == "no rule triggered"
