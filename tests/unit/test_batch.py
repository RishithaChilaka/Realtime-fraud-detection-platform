from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pytest

from src.api.batch import (
    RowError,
    ScoredTransaction,
    bucket_timeseries,
    build_dashboard_payload,
    parse_transactions_csv,
    score_batch,
)
from src.api.fallback import RuleBasedFallback
from src.common.config import Settings

pytestmark = pytest.mark.unit

BASE_TIME = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)

CSV_HEADER = (
    "transaction_id,card_id,user_id,amount,currency,merchant_id,merchant_category,"
    "transaction_type,channel,latitude,longitude,country,device_id,ip_address,event_time\n"
)


def _csv_row(**overrides) -> str:
    defaults = dict(
        transaction_id="txn_1",
        card_id="card_1",
        user_id="user_1",
        amount="50.00",
        currency="USD",
        merchant_id="merchant_1",
        merchant_category="grocery",
        transaction_type="purchase",
        channel="in_store",
        latitude="37.7749",
        longitude="-122.4194",
        country="US",
        device_id="device_1",
        ip_address="203.0.113.5",
        event_time="2026-08-06T12:00:00Z",
    )
    defaults.update(overrides)
    return ",".join(
        str(defaults[k])
        for k in [
            "transaction_id",
            "card_id",
            "user_id",
            "amount",
            "currency",
            "merchant_id",
            "merchant_category",
            "transaction_type",
            "channel",
            "latitude",
            "longitude",
            "country",
            "device_id",
            "ip_address",
            "event_time",
        ]
    )


@pytest.fixture
def settings():
    return Settings(
        fallback_velocity_5min_threshold=10,
        fallback_amount_zscore_threshold=5.0,
        fallback_impossible_travel_speed_kmh=900.0,
    )


@dataclass
class _FakeModelState:
    """Duck-typed stand-in for `ModelState` exposing only what
    `score_batch` actually reads (`is_model_available`, `model`,
    `fallback`) -- avoids constructing a real `ModelState`, which would
    otherwise reach out to Redis and MLflow just to run a pure-logic unit
    test."""

    is_model_available: bool
    fallback: RuleBasedFallback
    model: Optional[object] = None
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    run_id: Optional[str] = None


@pytest.fixture
def fallback_state(settings):
    return _FakeModelState(is_model_available=False, fallback=RuleBasedFallback(settings))


class _StubModel:
    """`predict_proba` stub returning a fixed fraud probability regardless
    of input, so ML-path tests don't need a real trained model."""

    def __init__(self, fraud_proba: float):
        self._fraud_proba = fraud_proba

    def predict_proba(self, X):
        n = len(X)
        return np.array([[1 - self._fraud_proba, self._fraud_proba]] * n)


@pytest.fixture
def ml_state(settings):
    return _FakeModelState(
        is_model_available=True,
        fallback=RuleBasedFallback(settings),
        model=_StubModel(0.9),
        model_name="fraud_xgboost",
        model_version="7",
        run_id="run_abc123",
    )


class TestParseTransactionsCsv:
    def test_parses_valid_rows(self):
        rows = _csv_row() + "\n" + _csv_row(transaction_id="txn_2") + "\n"
        content = (CSV_HEADER + rows).encode()

        transactions, row_errors, rows_in_file = parse_transactions_csv(content)

        assert rows_in_file == 2
        assert len(transactions) == 2
        assert row_errors == []
        assert {t.transaction_id for t in transactions} == {"txn_1", "txn_2"}

    def test_skips_malformed_row_and_reports_row_number(self):
        content = (
            CSV_HEADER
            + _csv_row()
            + "\n"
            + _csv_row(transaction_id="txn_bad", amount="not_a_number")
            + "\n"
            + _csv_row(transaction_id="txn_3")
            + "\n"
        ).encode()

        transactions, row_errors, rows_in_file = parse_transactions_csv(content)

        assert rows_in_file == 3
        assert len(transactions) == 2
        assert len(row_errors) == 1
        assert row_errors[0].row_number == 3  # header is row 1, so the 2nd data row is row 3
        assert isinstance(row_errors[0], RowError)

    def test_missing_required_column_raises(self):
        header = CSV_HEADER.replace("country,", "")
        content = (header + _csv_row().replace(",US,", ",")).encode()

        with pytest.raises(ValueError, match="country"):
            parse_transactions_csv(content)


class TestScoreBatch:
    def test_velocity_abuse_within_uploaded_file_is_detected(
        self, fallback_state, settings, make_transaction
    ):
        # 12 transactions for the same card within a 2-minute window, entirely
        # from the uploaded file itself -- score_batch must build this
        # history in memory (there is no Redis in this test) for the rule to
        # trigger on the later transactions.
        txns = [
            make_transaction(
                transaction_id=f"txn_{i}",
                card_id="card_burst",
                event_time=BASE_TIME + timedelta(seconds=i * 10),
            )
            for i in range(12)
        ]

        results = score_batch(fallback_state, settings, txns)

        assert len(results) == 12
        last = results[-1]
        assert last.reason_tag == "high_velocity"
        assert last.routed_to_review is True
        first = results[0]
        assert first.reason_tag == "none"  # no history yet on the first transaction

    def test_ml_source_recorded_when_model_available(self, ml_state, settings, make_transaction):
        txn = make_transaction()

        results = score_batch(ml_state, settings, [txn])

        assert results[0].model_source == "ml"
        assert results[0].fraud_score == pytest.approx(0.9)
        assert results[0].decision == "block"

    def test_history_is_scoped_per_card(self, fallback_state, settings, make_transaction):
        # Card A gets a velocity burst; card B has a single, unrelated
        # transaction in the same file -- card B must not be affected by
        # card A's history.
        burst = [
            make_transaction(
                transaction_id=f"txn_a_{i}",
                card_id="card_a",
                event_time=BASE_TIME + timedelta(seconds=i * 10),
            )
            for i in range(12)
        ]
        other = make_transaction(transaction_id="txn_b_1", card_id="card_b", event_time=BASE_TIME)

        results = score_batch(fallback_state, settings, burst + [other])
        by_id = {r.txn.transaction_id: r for r in results}

        assert by_id["txn_b_1"].reason_tag == "none"
        assert by_id["txn_b_1"].decision == "approve"


class TestBucketTimeseries:
    def _scored(self, event_time, routed_to_review, make_transaction, fallback_state, settings):
        txn = make_transaction(event_time=event_time)
        return ScoredTransaction(
            txn=txn,
            fraud_score=0.5,
            risk_level="medium",
            decision="review",
            routed_to_review=routed_to_review,
            model_source="fallback_rules",
            reason_tag="none",
            reason_label="No signal",
        )

    def test_buckets_span_the_files_own_time_range(
        self, make_transaction, fallback_state, settings
    ):
        later = BASE_TIME + timedelta(hours=1)
        scored = [
            self._scored(BASE_TIME, True, make_transaction, fallback_state, settings),
            self._scored(later, True, make_transaction, fallback_state, settings),
        ]

        buckets = bucket_timeseries(scored, num_buckets=4)

        assert len(buckets) == 4
        assert sum(b["alert_count"] for b in buckets) == 2

    def test_empty_input_returns_empty_list(self):
        assert bucket_timeseries([]) == []


class TestBuildDashboardPayload:
    def _row(self, make_transaction, *, amount, decision, routed, reason_tag, reason_label, score):
        txn = make_transaction(amount=amount)
        return ScoredTransaction(
            txn=txn,
            fraud_score=score,
            risk_level="high" if decision == "block" else "medium",
            decision=decision,
            routed_to_review=routed,
            model_source="fallback_rules",
            reason_tag=reason_tag,
            reason_label=reason_label,
        )

    def test_kpi_math_and_reason_breakdown(self, fallback_state, settings, make_transaction):
        scored = [
            self._row(
                make_transaction,
                amount=100.0,
                decision="block",
                routed=True,
                reason_tag="high_velocity",
                reason_label="High velocity",
                score=0.95,
            ),
            self._row(
                make_transaction,
                amount=200.0,
                decision="block",
                routed=True,
                reason_tag="high_velocity",
                reason_label="High velocity",
                score=0.91,
            ),
            self._row(
                make_transaction,
                amount=50.0,
                decision="review",
                routed=True,
                reason_tag="amount_outlier",
                reason_label="Amount outlier",
                score=0.5,
            ),
            self._row(
                make_transaction,
                amount=10.0,
                decision="approve",
                routed=False,
                reason_tag="none",
                reason_label="No signal",
                score=0.05,
            ),
        ]

        payload = build_dashboard_payload(
            state=fallback_state,
            settings=settings,
            scored=scored,
            row_errors=[],
            rows_in_file=4,
            truncated=False,
            model_metrics=None,
        )

        kpis = payload["kpis"]
        assert kpis["total_transactions"] == 4
        assert kpis["fraudulent_transactions"] == 2
        assert kpis["blocked_amount"] == pytest.approx(300.0)
        assert kpis["alerts_triggered"] == 3
        assert kpis["fraud_rate_pct"] == pytest.approx(50.0)

        reasons = payload["reasons"]
        assert len(reasons) == 1  # only "block" rows count toward the reason breakdown
        assert reasons[0]["reason_tag"] == "high_velocity"
        assert reasons[0]["count"] == 2
        assert reasons[0]["pct"] == pytest.approx(100.0)

        # top_alerts is every routed_to_review row, highest score first
        scores = [a["fraud_score"] for a in payload["top_alerts"]]
        assert scores == sorted(scores, reverse=True)
        assert len(payload["top_alerts"]) == 3

    def test_model_performance_reflects_fallback_when_no_model(
        self, fallback_state, settings, make_transaction
    ):
        payload = build_dashboard_payload(
            state=fallback_state,
            settings=settings,
            scored=[],
            row_errors=[],
            rows_in_file=0,
            truncated=False,
            model_metrics=None,
        )

        assert payload["model_performance"]["source"] == "fallback_active"

    def test_model_performance_uses_run_metrics_when_available(
        self, ml_state, settings, make_transaction
    ):
        payload = build_dashboard_payload(
            state=ml_state,
            settings=settings,
            scored=[],
            row_errors=[],
            rows_in_file=0,
            truncated=False,
            model_metrics={"precision": 0.96, "recall": 0.93, "f1": 0.94, "roc_auc": 0.97},
        )

        mp = payload["model_performance"]
        assert mp["source"] == "last_validated_run"
        assert mp["precision"] == 0.96
        assert mp["model_name"] == "fraud_xgboost"
