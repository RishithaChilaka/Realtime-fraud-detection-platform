"""
Pure feature-engineering logic.

Everything here is plain Python with no Spark or I/O dependency, on
purpose: `SparkStreamProcessor` (spark_consumer.py) calls these same
functions from inside a `foreachBatch`/pandas UDF so the exact logic that
is unit tested is the logic that runs in the streaming job — there is no
parallel "shadow" implementation to drift out of sync.

Given a card's recent transaction history plus the incoming transaction,
`compute_features` returns the feature vector a fraud model would score.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import fmean, pstdev
from typing import Optional, Sequence

from src.common.schemas import Transaction

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class FeatureVector:
    card_id: str
    as_of: datetime
    txn_count_1h: int
    txn_count_24h: int
    avg_amount_1h: float
    avg_amount_24h: float
    sum_amount_1h: float
    sum_amount_24h: float
    distinct_merchants_24h: int
    velocity_5min: int
    seconds_since_last_txn: Optional[float]
    distance_from_last_txn_km: Optional[float]
    implied_speed_kmh: Optional[float]
    amount_zscore_24h: float
    is_new_device: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        return d


def _in_window(
    history: Sequence[Transaction], as_of: datetime, window: timedelta
) -> list[Transaction]:
    lower = as_of - window
    return [t for t in history if lower <= t.event_time <= as_of]


def compute_features(history: Sequence[Transaction], current: Transaction) -> FeatureVector:
    """Compute the feature vector for `current`, using `history` (prior
    transactions for the same card, any order, `current` NOT included).

    Design notes:
    - 1h/24h windows are trailing windows ending at `current.event_time`.
    - `amount_zscore_24h` falls back to 0.0 when there's not enough history
      to estimate a standard deviation (fewer than 2 prior transactions),
      rather than dividing by zero.
    - `velocity_5min` counts transactions (including `current`) within the
      trailing 5 minutes -- this is the core rapid-succession / card-testing
      signal.
    """
    as_of = current.event_time
    past = sorted(
        (t for t in history if t.transaction_id != current.transaction_id),
        key=lambda t: t.event_time,
    )

    window_1h = _in_window(past, as_of, timedelta(hours=1))
    window_24h = _in_window(past, as_of, timedelta(hours=24))
    window_5min = _in_window(past, as_of, timedelta(minutes=5)) + [current]

    amounts_1h = [t.amount for t in window_1h] + [current.amount]
    amounts_24h = [t.amount for t in window_24h] + [current.amount]

    last_txn = past[-1] if past else None
    if last_txn is not None:
        seconds_since_last = (as_of - last_txn.event_time).total_seconds()
        distance_km = haversine_km(
            last_txn.latitude, last_txn.longitude, current.latitude, current.longitude
        )
        implied_speed = (
            (distance_km / (seconds_since_last / 3600)) if seconds_since_last > 0 else None
        )
    else:
        seconds_since_last = None
        distance_km = None
        implied_speed = None

    if len(amounts_24h) >= 3:
        history_only = amounts_24h[:-1]
        mean_24h = fmean(history_only)
        std_24h = pstdev(history_only)
        zscore = (current.amount - mean_24h) / std_24h if std_24h > 0 else 0.0
    else:
        zscore = 0.0

    known_devices = {t.device_id for t in past if t.device_id}
    is_new_device = (
        bool(current.device_id) and current.device_id not in known_devices and bool(known_devices)
    )

    return FeatureVector(
        card_id=current.card_id,
        as_of=as_of,
        txn_count_1h=len(amounts_1h),
        txn_count_24h=len(amounts_24h),
        avg_amount_1h=round(fmean(amounts_1h), 2),
        avg_amount_24h=round(fmean(amounts_24h), 2),
        sum_amount_1h=round(sum(amounts_1h), 2),
        sum_amount_24h=round(sum(amounts_24h), 2),
        distinct_merchants_24h=len({t.merchant_id for t in window_24h} | {current.merchant_id}),
        velocity_5min=len(window_5min),
        seconds_since_last_txn=seconds_since_last,
        distance_from_last_txn_km=round(distance_km, 2) if distance_km is not None else None,
        implied_speed_kmh=round(implied_speed, 2) if implied_speed is not None else None,
        amount_zscore_24h=round(zscore, 4),
        is_new_device=is_new_device,
    )


def is_impossible_travel(
    feature_vector: FeatureVector, max_plausible_speed_kmh: float = 900.0
) -> bool:
    """A simple, explainable rule built on top of the feature vector:
    commercial flight speed is ~900 km/h, so an implied speed above that
    between two consecutive card-present-ish events is physically
    implausible and a strong fraud signal."""
    if feature_vector.implied_speed_kmh is None:
        return False
    return feature_vector.implied_speed_kmh > max_plausible_speed_kmh


def is_velocity_abuse(feature_vector: FeatureVector, max_txns_per_5min: int = 5) -> bool:
    return feature_vector.velocity_5min > max_txns_per_5min
