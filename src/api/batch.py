"""
Batch scoring for uploaded transaction files (the dashboard's "upload a
file, see results" flow -- see `src/api/routes/batch.py`).

Deliberately reuses the exact same feature engineering (`compute_features`),
model scoring (`ModelState.model` / `RuleBasedFallback`), and routing
(`decide`) code paths as real-time `/score` -- there is no parallel
"batch scoring logic" to drift out of sync with the live path. The one
difference is the transaction history `compute_features` reads from: live
scoring reads/writes `RedisFeatureStore`, batch scoring builds a
throwaway, in-memory history from the uploaded file itself and never
touches Redis or Postgres. That was a deliberate choice (see the chat
where this was scoped) -- an upload is a one-off "what would the platform
have done with this data" analysis, not live traffic, so it shouldn't
pollute the feature store, the prediction audit trail, or the analyst
review queue.

Because batch transactions build history from each other (sorted by
event_time, per card_id) rather than from Redis, a file with realistic
per-card transaction sequences produces much more meaningful rolling-window
features (velocity, amount z-score, impossible travel) than scoring each
row in isolation would.
"""

from __future__ import annotations

import io
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import pandas as pd
from pydantic import ValidationError

from src.api.decision import decide
from src.api.fallback import FallbackResult
from src.api.inference import ModelState
from src.common.config import Settings
from src.common.schemas import Transaction
from src.feature_engineering.features import (
    FeatureVector,
    compute_features,
    is_impossible_travel,
    is_velocity_abuse,
)
from src.ml.model_features import build_feature_row, rows_to_dataframe, select_model_matrix

MAX_BATCH_ROWS = 5000  # keeps an interactive upload's latency reasonable


@dataclass(frozen=True)
class RowError:
    row_number: int  # 1-indexed, matches a spreadsheet's row numbering (header = row 1)
    error: str


@dataclass(frozen=True)
class ScoredTransaction:
    txn: Transaction
    fraud_score: float
    risk_level: str
    decision: str
    routed_to_review: bool
    model_source: str
    reason_tag: str
    reason_label: str


REQUIRED_COLUMNS = [
    "transaction_id",
    "card_id",
    "user_id",
    "amount",
    "merchant_id",
    "merchant_category",
    "transaction_type",
    "channel",
    "latitude",
    "longitude",
    "country",
    "event_time",
]


def parse_transactions_csv(content: bytes) -> tuple[list[Transaction], list[RowError], int]:
    """Parse an uploaded CSV into validated `Transaction`s.

    Returns `(transactions, row_errors, rows_in_file)`. Malformed rows are
    skipped (and reported in `row_errors`) rather than failing the whole
    upload -- a 4,000-row file with 3 bad rows should still score the other
    3,997.
    """
    df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
    rows_in_file = len(df)

    missing_columns = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_columns:
        raise ValueError(
            "CSV is missing required column(s): "
            + ", ".join(missing_columns)
            + ". Download the template from GET /batch/template for the expected shape."
        )

    transactions: list[Transaction] = []
    row_errors: list[RowError] = []

    for i, raw_row in enumerate(df.to_dict(orient="records")):
        row_number = i + 2  # +1 for 0-index, +1 for the header row
        row = {k: (v if v != "" else None) for k, v in raw_row.items()}
        try:
            row["amount"] = float(row["amount"]) if row.get("amount") is not None else None
            row["latitude"] = float(row["latitude"]) if row.get("latitude") is not None else None
            row["longitude"] = float(row["longitude"]) if row.get("longitude") is not None else None
            if row.get("is_simulated_fraud") is not None:
                row["is_simulated_fraud"] = str(row["is_simulated_fraud"]).strip().lower() in (
                    "true",
                    "1",
                    "yes",
                )
            txn = Transaction(**row)
            transactions.append(txn)
        except (ValidationError, ValueError, TypeError) as exc:
            row_errors.append(RowError(row_number=row_number, error=str(exc).splitlines()[0]))

    return transactions, row_errors, rows_in_file


def _derive_reason(fv: FeatureVector, decision: str, settings: Settings) -> tuple[str, str]:
    """A short, explainable tag for *why* a row looks the way it does --
    built from the same rule primitives `RuleBasedFallback` uses, so it's
    grounded in real, already-computed signals rather than an invented
    taxonomy. When the ML model flags a row that none of these simple
    rules would catch, that's tagged separately ("model signal") -- which
    is, honestly, the interesting case: it's exactly why a learned model
    earns its place over rules alone.
    """
    if is_velocity_abuse(fv, max_txns_per_5min=settings.fallback_velocity_5min_threshold):
        return "high_velocity", "High velocity"
    if fv.amount_zscore_24h >= settings.fallback_amount_zscore_threshold:
        return "amount_outlier", "Amount outlier"
    if is_impossible_travel(
        fv, max_plausible_speed_kmh=settings.fallback_impossible_travel_speed_kmh
    ):
        return "impossible_travel", "Impossible travel"
    if fv.is_new_device and fv.avg_amount_1h > 0 and decision != "approve":
        return "new_device_high_spend", "New device + high spend"
    if decision != "approve":
        return "model_signal", "Model signal (no rule match)"
    return "none", "No signal"


def score_batch(
    state: ModelState, settings: Settings, transactions: list[Transaction]
) -> list[ScoredTransaction]:
    """Score every transaction using an in-memory, per-card history built
    from the file itself (see module docstring) -- never Redis."""
    ordered = sorted(transactions, key=lambda t: t.event_time)
    history: dict[str, list[Transaction]] = defaultdict(list)
    results: list[ScoredTransaction] = []

    for txn in ordered:
        hist = history[txn.card_id]
        feature_vector = compute_features(hist, txn)
        row_dict = build_feature_row(feature_vector, txn)
        feature_row = select_model_matrix(rows_to_dataframe([row_dict]))

        if state.is_model_available:
            try:
                proba = state.model.predict_proba(feature_row)[:, 1]
                score = float(proba[0])
                model_source = "ml"
            except Exception:
                fallback_result: FallbackResult = state.fallback.score(feature_vector)
                score = fallback_result.score
                model_source = "fallback_rules"
        else:
            fallback_result = state.fallback.score(feature_vector)
            score = fallback_result.score
            model_source = "fallback_rules"

        routing = decide(score, settings)
        reason_tag, reason_label = _derive_reason(feature_vector, routing.decision, settings)

        results.append(
            ScoredTransaction(
                txn=txn,
                fraud_score=score,
                risk_level=routing.risk_level,
                decision=routing.decision,
                routed_to_review=routing.routed_to_review,
                model_source=model_source,
                reason_tag=reason_tag,
                reason_label=reason_label,
            )
        )
        hist.append(txn)

    return results


def bucket_timeseries(scored: list[ScoredTransaction], num_buckets: int = 12) -> list[dict]:
    """Bucket `routed_to_review` counts evenly across the file's own
    event_time range -- there's no "now" for an uploaded file, so the
    x-axis is the data's own timeline, not a live clock."""
    if not scored:
        return []
    times = [s.txn.event_time for s in scored]
    start, end = min(times), max(times)
    span = (end - start).total_seconds() or 1.0
    bucket_seconds = span / num_buckets

    buckets = [0] * num_buckets
    for s in scored:
        if not s.routed_to_review:
            continue
        offset = (s.txn.event_time - start).total_seconds()
        idx = min(int(offset / bucket_seconds), num_buckets - 1)
        buckets[idx] += 1

    same_day = start.date() == end.date()
    labels = []
    for i in range(num_buckets):
        bucket_time = start + (end - start) * (i / max(num_buckets - 1, 1))
        labels.append(bucket_time.strftime("%H:%M") if same_day else bucket_time.strftime("%m/%d"))

    return [{"label": labels[i], "alert_count": buckets[i]} for i in range(num_buckets)]


def build_dashboard_payload(
    state: ModelState,
    settings: Settings,
    scored: list[ScoredTransaction],
    row_errors: list[RowError],
    rows_in_file: int,
    truncated: bool,
    model_metrics: Optional[dict] = None,
) -> dict:
    total = len(scored)
    fraud_rows = [s for s in scored if s.decision == "block"]
    alert_rows = [s for s in scored if s.routed_to_review]

    fraud_count = len(fraud_rows)
    blocked_amount = sum(s.txn.amount for s in fraud_rows)
    fraud_rate = (fraud_count / total * 100) if total else 0.0

    reason_counts: dict[str, dict] = {}
    for s in fraud_rows:
        entry = reason_counts.setdefault(s.reason_tag, {"reason_label": s.reason_label, "count": 0})
        entry["count"] += 1
    reasons = [
        {
            "reason_tag": tag,
            "reason_label": data["reason_label"],
            "count": data["count"],
            "pct": round(data["count"] / fraud_count * 100, 1) if fraud_count else 0.0,
        }
        for tag, data in sorted(reason_counts.items(), key=lambda kv: kv[1]["count"], reverse=True)
    ]

    top_alerts = sorted(alert_rows, key=lambda s: s.fraud_score, reverse=True)[:200]
    geo_points = sorted(alert_rows, key=lambda s: s.fraud_score, reverse=True)[:300]

    model_performance = None
    if state.is_model_available:
        if model_metrics:
            model_performance = {
                "source": "last_validated_run",
                "model_name": state.model_name,
                "model_version": state.model_version,
                "model_type": "Classification",
                "precision": model_metrics.get("precision"),
                "recall": model_metrics.get("recall"),
                "f1": model_metrics.get("f1"),
                "roc_auc": model_metrics.get("roc_auc"),
            }
        else:
            model_performance = {
                "source": "unavailable",
                "model_name": state.model_name,
                "model_version": state.model_version,
                "model_type": "Classification",
                "precision": None,
                "recall": None,
                "f1": None,
                "roc_auc": None,
            }
    else:
        model_performance = {
            "source": "fallback_active",
            "model_name": state.fallback.MODEL_NAME,
            "model_version": state.fallback.MODEL_VERSION,
            "model_type": "Rule-based",
            "precision": None,
            "recall": None,
            "f1": None,
            "roc_auc": None,
        }

    return {
        "kpis": {
            "total_transactions": total,
            "fraudulent_transactions": fraud_count,
            "blocked_amount": round(blocked_amount, 2),
            "alerts_triggered": len(alert_rows),
            "fraud_rate_pct": round(fraud_rate, 2),
        },
        "timeseries": bucket_timeseries(scored),
        "reasons": reasons,
        "geo_points": [
            {
                "transaction_id": s.txn.transaction_id,
                "latitude": s.txn.latitude,
                "longitude": s.txn.longitude,
                "country": s.txn.country,
                "fraud_score": round(s.fraud_score, 4),
                "decision": s.decision,
            }
            for s in geo_points
        ],
        "top_alerts": [
            {
                "transaction_id": s.txn.transaction_id,
                "event_time": s.txn.event_time.isoformat(),
                "card_id": s.txn.card_id,
                "user_id": s.txn.user_id,
                "country": s.txn.country,
                "reason_label": s.reason_label,
                "reason_tag": s.reason_tag,
                "amount": s.txn.amount,
                "currency": s.txn.currency,
                "fraud_score": round(s.fraud_score, 4),
                "risk_level": s.risk_level,
                "decision": s.decision,
                "model_source": s.model_source,
            }
            for s in top_alerts
        ],
        "model_performance": model_performance,
        "upload_meta": {
            "rows_in_file": rows_in_file,
            "rows_scored": total,
            "rows_skipped": len(row_errors),
            "truncated": truncated,
            "max_batch_rows": MAX_BATCH_ROWS,
            "row_errors": [{"row_number": e.row_number, "error": e.error} for e in row_errors[:50]],
        },
    }
