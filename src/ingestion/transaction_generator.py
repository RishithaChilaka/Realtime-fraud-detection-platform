"""
Realistic credit-card transaction generator.

Generates a population of synthetic cardholders with "normal" spending
profiles (home country, typical merchant categories, typical amounts),
then emits transactions that are mostly normal but periodically inject
labeled edge cases that a fraud pipeline needs to be able to see:

- high_value: amount far outside the cardholder's normal range
- impossible_travel: two transactions for the same card in geographically
  distant locations within a short time window
- velocity_abuse: many transactions for the same card in rapid succession
- new_device_high_value: unfamiliar device paired with a large amount

The generator is deterministic given a seed, which makes it usable both
for live demo traffic and for reproducible integration tests.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from faker import Faker

from src.common.schemas import Transaction, TransactionChannel, TransactionType

_MERCHANT_CATEGORIES = [
    "grocery",
    "electronics",
    "restaurant",
    "gas_station",
    "travel",
    "entertainment",
    "utilities",
    "clothing",
    "pharmacy",
    "online_marketplace",
]

# (country, lat, lon) anchor points used to build "home" locations and to
# pick a deliberately distant location for impossible-travel edge cases.
_GEO_ANCHORS = [
    ("US", 37.7749, -122.4194),   # San Francisco
    ("US", 40.7128, -74.0060),    # New York
    ("GB", 51.5074, -0.1278),     # London
    ("DE", 52.5200, 13.4050),     # Berlin
    ("JP", 35.6762, 139.6503),    # Tokyo
    ("AU", -33.8688, 151.2093),   # Sydney
    ("BR", -23.5505, -46.6333),   # Sao Paulo
    ("ZA", -26.2041, 28.0473),    # Johannesburg
]


@dataclass
class CardholderProfile:
    """A synthetic cardholder's baseline behavior, used to make normal
    transactions look normal and edge cases look like clear outliers."""

    card_id: str
    user_id: str
    home_country: str
    home_lat: float
    home_lon: float
    typical_amount_mean: float
    typical_amount_std: float
    preferred_categories: list[str] = field(default_factory=list)
    device_ids: list[str] = field(default_factory=list)


class TransactionGenerator:
    """Produces a stream of `Transaction` objects, mixing normal activity
    with a configurable ratio of labeled fraud-like edge cases."""

    def __init__(
        self,
        num_cardholders: int = 500,
        edge_case_ratio: float = 0.05,
        seed: Optional[int] = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._faker = Faker()
        Faker.seed(seed)
        self.edge_case_ratio = edge_case_ratio
        self.profiles = [self._build_profile() for _ in range(num_cardholders)]

    def _build_profile(self) -> CardholderProfile:
        country, lat, lon = self._rng.choice(_GEO_ANCHORS)
        # jitter the anchor so cardholders aren't all at the exact same point
        lat += self._rng.uniform(-0.5, 0.5)
        lon += self._rng.uniform(-0.5, 0.5)
        return CardholderProfile(
            card_id=f"card_{uuid.uuid4().hex[:12]}",
            user_id=f"user_{uuid.uuid4().hex[:12]}",
            home_country=country,
            home_lat=lat,
            home_lon=lon,
            typical_amount_mean=self._rng.uniform(15, 150),
            typical_amount_std=self._rng.uniform(5, 40),
            preferred_categories=self._rng.sample(_MERCHANT_CATEGORIES, k=3),
            device_ids=[f"device_{uuid.uuid4().hex[:10]}" for _ in range(self._rng.randint(1, 3))],
        )

    def _normal_transaction(
        self, profile: CardholderProfile, event_time: datetime
    ) -> Transaction:
        amount = max(1.0, self._rng.gauss(profile.typical_amount_mean, profile.typical_amount_std))
        return Transaction(
            transaction_id=f"txn_{uuid.uuid4().hex}",
            card_id=profile.card_id,
            user_id=profile.user_id,
            amount=round(amount, 2),
            merchant_id=f"merchant_{self._rng.randint(1, 5000)}",
            merchant_category=self._rng.choice(profile.preferred_categories),
            transaction_type=TransactionType.PURCHASE,
            channel=self._rng.choice(list(TransactionChannel)),
            latitude=profile.home_lat + self._rng.uniform(-0.1, 0.1),
            longitude=profile.home_lon + self._rng.uniform(-0.1, 0.1),
            country=profile.home_country,
            device_id=self._rng.choice(profile.device_ids),
            ip_address=self._faker.ipv4_public(),
            event_time=event_time,
            is_simulated_fraud=False,
        )

    def _high_value_transaction(
        self, profile: CardholderProfile, event_time: datetime
    ) -> Transaction:
        txn = self._normal_transaction(profile, event_time)
        spike = profile.typical_amount_mean + profile.typical_amount_std * self._rng.uniform(15, 40)
        return txn.model_copy(update={"amount": round(spike, 2), "is_simulated_fraud": True})

    def _impossible_travel_pair(
        self, profile: CardholderProfile, event_time: datetime
    ) -> list[Transaction]:
        first = self._normal_transaction(profile, event_time)
        far_country, far_lat, far_lon = self._rng.choice(
            [g for g in _GEO_ANCHORS if g[0] != profile.home_country]
        )
        second = first.model_copy(
            update={
                "transaction_id": f"txn_{uuid.uuid4().hex}",
                "latitude": far_lat,
                "longitude": far_lon,
                "country": far_country,
                "event_time": event_time + timedelta(minutes=self._rng.randint(2, 20)),
                "is_simulated_fraud": True,
            }
        )
        return [first, second]

    def _velocity_abuse_burst(
        self, profile: CardholderProfile, event_time: datetime
    ) -> list[Transaction]:
        burst_size = self._rng.randint(6, 15)
        txns = []
        for i in range(burst_size):
            t = self._normal_transaction(profile, event_time + timedelta(seconds=i * self._rng.uniform(1, 8)))
            t = t.model_copy(update={"is_simulated_fraud": True})
            txns.append(t)
        return txns

    def _new_device_high_value(
        self, profile: CardholderProfile, event_time: datetime
    ) -> Transaction:
        txn = self._high_value_transaction(profile, event_time)
        return txn.model_copy(update={"device_id": f"device_{uuid.uuid4().hex[:10]}"})

    def stream(self, n: int, start_time: Optional[datetime] = None) -> Iterator[Transaction]:
        """Yield roughly `n` transactions (edge-case bursts may push the
        actual count slightly above `n`)."""
        clock = start_time or datetime.now(timezone.utc)
        emitted = 0
        while emitted < n:
            profile = self._rng.choice(self.profiles)
            clock += timedelta(milliseconds=self._rng.uniform(5, 250))
            roll = self._rng.random()

            if roll < self.edge_case_ratio * 0.35:
                for t in self._velocity_abuse_burst(profile, clock):
                    yield t
                    emitted += 1
            elif roll < self.edge_case_ratio * 0.65:
                for t in self._impossible_travel_pair(profile, clock):
                    yield t
                    emitted += 1
            elif roll < self.edge_case_ratio * 0.85:
                yield self._high_value_transaction(profile, clock)
                emitted += 1
            elif roll < self.edge_case_ratio:
                yield self._new_device_high_value(profile, clock)
                emitted += 1
            else:
                yield self._normal_transaction(profile, clock)
                emitted += 1
