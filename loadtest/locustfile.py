"""
Locust load test for the `/score` endpoint (and, at lighter weight,
`/explain`), targeting the Phase 2 success criteria: 500 requests/sec at
p95 < 100ms.

Run against a live stack (`docker compose up -d`, wait for `/health` to
report a loaded model or fallback):

    locust -f loadtest/locustfile.py --host http://localhost:8080 \\
        --users 500 --spawn-rate 50 --run-time 3m --headless \\
        --csv loadtest/results/run1

Then summarize the CSV into the report with:

    python loadtest/summarize_results.py loadtest/results/run1_stats.csv

`--users 500` with `wait_time` averaging ~1s per user approximates ~500
RPS at steady state; for a harder guarantee of sustained throughput
regardless of response latency, run in `--headless` mode with
`--users 500 --spawn-rate 500` against a host that can actually sustain
it, and confirm achieved RPS from the Locust summary (`Requests/s`) rather
than assuming user count == RPS.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone

from locust import HttpUser, between, task

_MERCHANT_CATEGORIES = ["grocery", "electronics", "restaurant", "gas_station", "travel", "entertainment"]
_CHANNELS = ["online", "in_store", "atm", "mobile"]
_COUNTRIES = [("US", 37.7749, -122.4194), ("GB", 51.5074, -0.1278), ("DE", 52.5200, 13.4050)]


def _random_transaction() -> dict:
    country, lat, lon = random.choice(_COUNTRIES)
    return {
        "transaction_id": f"txn_load_{uuid.uuid4().hex}",
        "card_id": f"card_load_{random.randint(1, 5000)}",
        "user_id": f"user_load_{random.randint(1, 5000)}",
        "amount": round(random.uniform(5, 500), 2),
        "currency": "USD",
        "merchant_id": f"merchant_{random.randint(1, 2000)}",
        "merchant_category": random.choice(_MERCHANT_CATEGORIES),
        "transaction_type": "purchase",
        "channel": random.choice(_CHANNELS),
        "latitude": lat + random.uniform(-0.1, 0.1),
        "longitude": lon + random.uniform(-0.1, 0.1),
        "country": country,
        "device_id": f"device_{random.randint(1, 3000)}",
        "ip_address": "203.0.113.5",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "is_simulated_fraud": False,
    }


class FraudScoringUser(HttpUser):
    wait_time = between(0.05, 0.2)

    @task(9)
    def score(self) -> None:
        self.client.post("/score", json=_random_transaction(), name="/score")

    @task(1)
    def explain(self) -> None:
        self.client.post("/explain", json=_random_transaction(), name="/explain")

    @task(1)
    def health(self) -> None:
        self.client.get("/health", name="/health")
