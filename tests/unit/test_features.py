from datetime import datetime, timedelta, timezone

import pytest

from src.feature_engineering.features import (
    compute_features,
    haversine_km,
    is_impossible_travel,
    is_velocity_abuse,
)

pytestmark = pytest.mark.unit

BASE_TIME = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
SF = (37.7749, -122.4194)
NY = (40.7128, -74.0060)


class TestHaversine:
    def test_same_point_is_zero_distance(self):
        assert haversine_km(*SF, *SF) == pytest.approx(0.0, abs=1e-6)

    def test_sf_to_ny_is_roughly_4130_km(self):
        # Known great-circle distance SF <-> NYC is ~4129 km.
        assert haversine_km(*SF, *NY) == pytest.approx(4129, rel=0.01)


class TestComputeFeaturesNoHistory:
    def test_first_transaction_has_zero_prior_counts(self, make_transaction):
        txn = make_transaction(event_time=BASE_TIME)
        fv = compute_features(history=[], current=txn)

        assert fv.txn_count_1h == 1
        assert fv.txn_count_24h == 1
        assert fv.avg_amount_1h == txn.amount
        assert fv.seconds_since_last_txn is None
        assert fv.distance_from_last_txn_km is None
        assert fv.implied_speed_kmh is None
        assert fv.amount_zscore_24h == 0.0
        assert fv.is_new_device is False


class TestComputeFeaturesWithHistory:
    def test_rolling_1h_count_excludes_older_transactions(self, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(minutes=90)),  # outside 1h
            make_transaction(event_time=BASE_TIME - timedelta(minutes=30)),  # inside 1h
        ]
        current = make_transaction(event_time=BASE_TIME)

        fv = compute_features(history, current)

        assert fv.txn_count_1h == 2  # 30-min-ago txn + current
        assert fv.txn_count_24h == 3  # both history txns + current

    def test_rolling_24h_count_excludes_transactions_older_than_24h(self, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(hours=25)),
            make_transaction(event_time=BASE_TIME - timedelta(hours=1)),
        ]
        current = make_transaction(event_time=BASE_TIME)

        fv = compute_features(history, current)

        assert fv.txn_count_24h == 2

    def test_avg_and_sum_amounts_1h(self, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(minutes=10), amount=100.0),
            make_transaction(event_time=BASE_TIME - timedelta(minutes=20), amount=200.0),
        ]
        current = make_transaction(event_time=BASE_TIME, amount=300.0)

        fv = compute_features(history, current)

        assert fv.sum_amount_1h == pytest.approx(600.0)
        assert fv.avg_amount_1h == pytest.approx(200.0)

    def test_seconds_since_last_and_distance(self, make_transaction):
        history = [
            make_transaction(
                event_time=BASE_TIME - timedelta(minutes=10),
                latitude=SF[0],
                longitude=SF[1],
            )
        ]
        current = make_transaction(event_time=BASE_TIME, latitude=SF[0], longitude=SF[1])

        fv = compute_features(history, current)

        assert fv.seconds_since_last_txn == pytest.approx(600.0)
        assert fv.distance_from_last_txn_km == pytest.approx(0.0, abs=1e-3)
        assert fv.implied_speed_kmh == pytest.approx(0.0, abs=1e-3)

    def test_velocity_5min_counts_rapid_succession(self, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(seconds=s))
            for s in (30, 60, 90, 120, 150)
        ]
        current = make_transaction(event_time=BASE_TIME)

        fv = compute_features(history, current)

        assert fv.velocity_5min == 6  # 5 history + current, all within 5 minutes

    def test_new_device_flagged_when_known_devices_exist(self, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(minutes=5), device_id="device_known")
        ]
        current = make_transaction(event_time=BASE_TIME, device_id="device_never_seen")

        fv = compute_features(history, current)

        assert fv.is_new_device is True

    def test_first_ever_device_is_not_flagged_new(self, make_transaction):
        current = make_transaction(event_time=BASE_TIME, device_id="device_first")
        fv = compute_features(history=[], current=current)
        assert fv.is_new_device is False

    def test_amount_zscore_flags_outlier_amount(self, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(hours=h), amount=amt)
            for h, amt in zip(range(1, 6), [50, 55, 45, 52, 48])
        ]
        # a $5,000 transaction against a ~$50 history should have a large positive z-score
        current = make_transaction(event_time=BASE_TIME, amount=5000.0)

        fv = compute_features(history, current)

        assert fv.amount_zscore_24h > 5


class TestFraudHeuristics:
    def test_impossible_travel_flagged_for_implausible_speed(self, make_transaction):
        history = [
            make_transaction(
                event_time=BASE_TIME - timedelta(minutes=5), latitude=SF[0], longitude=SF[1]
            )
        ]
        # SF -> NYC in 5 minutes is impossible (~4130km in 5 min => way over 900km/h)
        current = make_transaction(event_time=BASE_TIME, latitude=NY[0], longitude=NY[1])

        fv = compute_features(history, current)

        assert is_impossible_travel(fv) is True

    def test_plausible_travel_not_flagged(self, make_transaction):
        history = [
            make_transaction(
                event_time=BASE_TIME - timedelta(hours=6), latitude=SF[0], longitude=SF[1]
            )
        ]
        current = make_transaction(event_time=BASE_TIME, latitude=NY[0], longitude=NY[1])

        fv = compute_features(history, current)

        assert is_impossible_travel(fv) is False

    def test_velocity_abuse_flagged_above_threshold(self, make_transaction):
        history = [
            make_transaction(event_time=BASE_TIME - timedelta(seconds=s))
            for s in (10, 20, 30, 40, 50, 60)
        ]
        current = make_transaction(event_time=BASE_TIME)

        fv = compute_features(history, current)

        assert is_velocity_abuse(fv, max_txns_per_5min=5) is True

    def test_normal_activity_not_flagged_as_velocity_abuse(self, make_transaction):
        history = [make_transaction(event_time=BASE_TIME - timedelta(hours=2))]
        current = make_transaction(event_time=BASE_TIME)

        fv = compute_features(history, current)

        assert is_velocity_abuse(fv) is False
