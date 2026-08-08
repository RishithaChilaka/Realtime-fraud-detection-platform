"""
Integration test: FastAPI `/score` and `/explain` against a real
PostgreSQL and Redis (via testcontainers), with a small, genuinely-trained
XGBoost model injected in place of an MLflow-served one.

Training a real (if tiny) XGBClassifier here -- rather than a hand-rolled
stub with a fake `predict_proba` -- matters because `/explain` calls
`shap.TreeExplainer`, which introspects an actual tree structure; a stub
object would make the SHAP path untestable.

Requires Docker (testcontainers) and the full `requirements-dev.txt`
dependency set (xgboost, shap, fastapi, testcontainers[redis]). Skipped
automatically if Docker isn't reachable, same pattern as
tests/integration/test_pipeline.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers")
fastapi_testclient = pytest.importorskip("fastapi.testclient")
xgboost = pytest.importorskip("xgboost")


def _docker_available() -> bool:
    from docker.errors import DockerException

    import docker

    try:
        docker.from_env().ping()
        return True
    except (DockerException, Exception):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker daemon not available"),
]


@pytest.fixture(scope="module")
def postgres_container():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="module")
def redis_container():
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as r:
        yield r


@pytest.fixture
def test_settings(postgres_container, redis_container):
    from src.common.config import Settings

    return Settings(
        postgres_host=postgres_container.get_container_host_ip(),
        postgres_port=int(postgres_container.get_exposed_port(5432)),
        postgres_db=postgres_container.dbname,
        postgres_user=postgres_container.username,
        postgres_password=postgres_container.password,
        redis_host=redis_container.get_container_host_ip(),
        redis_port=int(redis_container.get_exposed_port(6379)),
    )


@pytest.fixture
def trained_model():
    """A real, tiny XGBoost model fit on random data with an arbitrary
    learnable rule -- enough structure for shap.TreeExplainer to compute
    genuine (if not meaningful) Shapley values against."""
    import xgboost as xgb

    from src.ml.model_features import FEATURE_COLUMNS

    rng = np.random.default_rng(42)
    X = pd.DataFrame(rng.random((300, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS)
    y = ((X["amount"] > 0.7) | (X["velocity_5min"] > 0.8)).astype(int)
    model = xgb.XGBClassifier(n_estimators=15, max_depth=3, random_state=42)
    model.fit(X, y)
    return model


@pytest.fixture
def client(test_settings, trained_model, monkeypatch):
    from fastapi.testclient import TestClient

    from src.ml import registry

    fake_loaded = registry.LoadedModel(
        model=trained_model,
        model_name="fraud_xgboost",
        model_version="test-1",
        stage="Production",
        flavor="xgboost",
    )
    monkeypatch.setattr(registry, "load_production_model", lambda settings=None: fake_loaded)
    monkeypatch.setattr("src.api.main.get_settings", lambda: test_settings)

    from src.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_no_model(test_settings, monkeypatch):
    """Client with no Production model available -- exercises the
    rule-based fallback path end to end through the real HTTP API."""
    from fastapi.testclient import TestClient

    from src.ml import registry

    monkeypatch.setattr(registry, "load_production_model", lambda settings=None: None)
    monkeypatch.setattr("src.api.main.get_settings", lambda: test_settings)

    from src.api.main import app

    with TestClient(app) as c:
        yield c


def _sample_transaction_payload(transaction_id: str = "txn_it_1") -> dict:
    return {
        "transaction_id": transaction_id,
        "card_id": "card_it_1",
        "user_id": "user_it_1",
        "amount": 42.50,
        "currency": "USD",
        "merchant_id": "merchant_1",
        "merchant_category": "grocery",
        "transaction_type": "purchase",
        "channel": "in_store",
        "latitude": 37.7749,
        "longitude": -122.4194,
        "country": "US",
        "device_id": "device_1",
        "ip_address": "203.0.113.5",
        "event_time": "2026-08-06T12:00:00Z",
        "is_simulated_fraud": False,
    }


class TestHealthEndpoint:
    def test_health_reports_model_loaded(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["model_loaded"] is True
        assert body["model_name"] == "fraud_xgboost"


class TestScoreEndpoint:
    def test_score_returns_valid_response_shape(self, client):
        resp = client.post("/score", json=_sample_transaction_payload())
        assert resp.status_code == 200
        body = resp.json()

        assert 0.0 <= body["fraud_score"] <= 1.0
        assert body["risk_level"] in ("low", "medium", "high")
        assert body["decision"] in ("approve", "review", "block")
        assert body["model_source"] == "ml"
        assert body["model_name"] == "fraud_xgboost"
        assert body["prediction_id"]
        assert body["latency_ms"] > 0

    def test_score_persists_prediction_audit_row(self, client, test_settings):
        resp = client.post("/score", json=_sample_transaction_payload("txn_it_audit"))
        prediction_id = resp.json()["prediction_id"]

        from src.storage.postgres_client import PostgresClient

        pg = PostgresClient(test_settings)
        record = pg.get_prediction(prediction_id)

        assert record is not None
        assert record["transaction_id"] == "txn_it_audit"

    def test_invalid_transaction_rejected_with_422(self, client):
        payload = _sample_transaction_payload()
        payload["amount"] = -5.0
        resp = client.post("/score", json=payload)
        assert resp.status_code == 422


class TestExplainEndpoint:
    def test_explain_returns_top_5_shap_features(self, client):
        resp = client.post("/explain", json=_sample_transaction_payload("txn_it_explain"))
        assert resp.status_code == 200
        body = resp.json()

        assert body["explanation_type"] == "shap"
        assert len(body["top_features"]) == 5
        assert body["base_value"] is not None
        for feature in body["top_features"]:
            assert "feature" in feature and "contribution" in feature and "value" in feature

    def test_explain_latency_is_reported(self, client):
        resp = client.post("/explain", json=_sample_transaction_payload("txn_it_explain2"))
        assert resp.json()["latency_ms"] > 0


class TestFallbackMode:
    """With no Production model registered, the API should still answer
    every request -- degraded to rule-based scoring -- rather than 500."""

    def test_health_reports_degraded(self, client_no_model):
        resp = client_no_model.get("/health")
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["model_loaded"] is False
        assert body["fallback_active"] is True

    def test_score_falls_back_to_rules(self, client_no_model):
        resp = client_no_model.post("/score", json=_sample_transaction_payload("txn_it_fallback"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["model_source"] == "fallback_rules"

    def test_explain_returns_rule_based_reason(self, client_no_model):
        payload = _sample_transaction_payload("txn_it_fallback_explain")
        resp = client_no_model.post("/explain", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["explanation_type"] == "rule_based"
        assert body["rule_based_reason"] is not None
