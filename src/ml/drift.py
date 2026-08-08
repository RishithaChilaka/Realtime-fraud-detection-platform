"""
Statistical drift detection: KS test and PSI (Population Stability Index)
on feature distributions and on the model's prediction-score distribution.

Both tests compare a "reference" distribution (the training set the
current Production model was trained on) against a "current" window
(recent live scoring inputs/outputs, pulled from the `predictions` audit
table by `dags/drift_detection_dag.py`). This module has zero I/O -- it
takes arrays in, returns structured results out -- so it's unit-testable
without a database and reusable from both the Airflow DAG and ad-hoc
notebooks/scripts.

Rule of thumb thresholds (configurable, not hardcoded as gospel):
  - KS test: p-value < 0.05 -> statistically significant distribution
    shift for that feature.
  - PSI: < 0.1 no significant shift, 0.1-0.25 moderate shift (watch),
    > 0.25 major shift (this is the industry-standard PSI banding used
    in credit risk / fraud modeling).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import numpy as np
import pandas as pd
from scipy import stats

if TYPE_CHECKING:
    from src.common.config import Settings


@dataclass(frozen=True)
class FeatureDriftResult:
    feature: str
    ks_statistic: float
    ks_p_value: float
    psi: float
    reference_mean: float
    current_mean: float
    is_drifted: bool  # True if either test crosses its threshold


@dataclass
class DriftReport:
    model_name: str
    reference_window: str
    current_window: str
    feature_results: list[FeatureDriftResult]
    score_ks_statistic: Optional[float] = None
    score_ks_p_value: Optional[float] = None
    score_psi: Optional[float] = None
    score_drifted: bool = False

    @property
    def drifted_features(self) -> list[str]:
        return [r.feature for r in self.feature_results if r.is_drifted]

    @property
    def any_drift_detected(self) -> bool:
        return bool(self.drifted_features) or self.score_drifted

    @property
    def max_psi(self) -> float:
        feature_psis = [r.psi for r in self.feature_results]
        all_psis = feature_psis + ([self.score_psi] if self.score_psi is not None else [])
        return max(all_psis) if all_psis else 0.0

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "reference_window": self.reference_window,
            "current_window": self.current_window,
            "any_drift_detected": self.any_drift_detected,
            "drifted_features": self.drifted_features,
            "max_psi": round(self.max_psi, 4),
            "score_ks_statistic": self.score_ks_statistic,
            "score_ks_p_value": self.score_ks_p_value,
            "score_psi": self.score_psi,
            "score_drifted": self.score_drifted,
            "features": [
                {
                    "feature": r.feature,
                    "ks_statistic": round(r.ks_statistic, 4),
                    "ks_p_value": round(r.ks_p_value, 6),
                    "psi": round(r.psi, 4),
                    "reference_mean": round(r.reference_mean, 4),
                    "current_mean": round(r.current_mean, 4),
                    "is_drifted": r.is_drifted,
                }
                for r in self.feature_results
            ],
        }


def population_stability_index(
    reference: np.ndarray, current: np.ndarray, buckets: int = 10
) -> float:
    """PSI = sum((current% - reference%) * ln(current% / reference%)) over
    quantile buckets fit on the reference distribution. Buckets are clipped
    to a small floor probability so a bucket with zero observations in
    either window doesn't produce -inf/NaN from log(0)."""
    reference = np.asarray(reference, dtype=float)
    current = np.asarray(current, dtype=float)
    reference = reference[~np.isnan(reference)]
    current = current[~np.isnan(current)]

    if len(reference) < buckets or len(current) == 0:
        return 0.0

    quantiles = np.linspace(0, 100, buckets + 1)
    breakpoints = np.unique(np.percentile(reference, quantiles))
    if len(breakpoints) < 3:
        # Reference distribution has too little variance to bucket meaningfully.
        return 0.0
    breakpoints[0] = -np.inf
    breakpoints[-1] = np.inf

    ref_counts, _ = np.histogram(reference, bins=breakpoints)
    cur_counts, _ = np.histogram(current, bins=breakpoints)

    ref_pct = np.clip(ref_counts / max(len(reference), 1), 1e-4, None)
    cur_pct = np.clip(cur_counts / max(len(current), 1), 1e-4, None)

    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def compute_feature_drift(
    reference_df: pd.DataFrame,
    current_df: pd.DataFrame,
    feature_columns: list[str],
    ks_p_value_threshold: float = 0.05,
    psi_threshold: float = 0.25,
) -> list[FeatureDriftResult]:
    """Run KS test + PSI for every column in `feature_columns` present in
    both frames. A feature is flagged drifted if EITHER test crosses its
    threshold -- KS catches distribution shape changes PSI can miss on
    small samples, PSI catches gradual mean/variance shift KS can under-
    power on for large samples."""
    results: list[FeatureDriftResult] = []
    for col in feature_columns:
        if col not in reference_df.columns or col not in current_df.columns:
            continue
        ref_values = reference_df[col].dropna().to_numpy(dtype=float)
        cur_values = current_df[col].dropna().to_numpy(dtype=float)
        if len(ref_values) < 2 or len(cur_values) < 2:
            continue

        ks_stat, ks_p = stats.ks_2samp(ref_values, cur_values)
        psi = population_stability_index(ref_values, cur_values)

        is_drifted = bool(ks_p < ks_p_value_threshold) or bool(psi > psi_threshold)

        results.append(
            FeatureDriftResult(
                feature=col,
                ks_statistic=float(ks_stat),
                ks_p_value=float(ks_p),
                psi=psi,
                reference_mean=float(np.mean(ref_values)),
                current_mean=float(np.mean(cur_values)),
                is_drifted=is_drifted,
            )
        )
    return results


def compute_score_drift(
    reference_scores: np.ndarray,
    current_scores: np.ndarray,
    ks_p_value_threshold: float = 0.05,
    psi_threshold: float = 0.25,
) -> tuple[float, float, float, bool]:
    """Same tests, applied to the model's output probability distribution
    rather than an input feature -- catches score-distribution shift even
    when no single input feature moved enough to trip its own threshold."""
    ref = np.asarray(reference_scores, dtype=float)
    cur = np.asarray(current_scores, dtype=float)
    if len(ref) < 2 or len(cur) < 2:
        return 0.0, 1.0, 0.0, False

    ks_stat, ks_p = stats.ks_2samp(ref, cur)
    psi = population_stability_index(ref, cur)
    drifted = bool(ks_p < ks_p_value_threshold) or bool(psi > psi_threshold)
    return float(ks_stat), float(ks_p), psi, drifted


def build_drift_report(
    model_name: str,
    reference_df: pd.DataFrame,
    current_df: pd.DataFrame,
    feature_columns: list[str],
    reference_window: str,
    current_window: str,
    reference_scores: Optional[np.ndarray] = None,
    current_scores: Optional[np.ndarray] = None,
) -> DriftReport:
    feature_results = compute_feature_drift(reference_df, current_df, feature_columns)

    score_ks_stat = score_ks_p = score_psi = None
    score_drifted = False
    if reference_scores is not None and current_scores is not None:
        score_ks_stat, score_ks_p, score_psi, score_drifted = compute_score_drift(
            reference_scores, current_scores
        )

    return DriftReport(
        model_name=model_name,
        reference_window=reference_window,
        current_window=current_window,
        feature_results=feature_results,
        score_ks_statistic=score_ks_stat,
        score_ks_p_value=score_ks_p,
        score_psi=score_psi,
        score_drifted=score_drifted,
    )


def push_drift_metrics(settings: "Settings", report: DriftReport) -> None:
    """Push the drift report to Prometheus Pushgateway so Grafana's model
    health dashboard and Alertmanager's drift rule can see it -- same
    push-based pattern as `src/monitoring/business_metrics.py`, for the
    same reason (this runs as a batch Airflow task, not a scraped
    process)."""
    from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

    registry = CollectorRegistry()

    Gauge(
        "fraud_drift_any_detected",
        "1 if any feature or the score distribution drifted, else 0",
        registry=registry,
    ).set(1 if report.any_drift_detected else 0)
    Gauge(
        "fraud_drift_max_psi",
        "Highest PSI across all monitored features + score",
        registry=registry,
    ).set(report.max_psi)
    Gauge(
        "fraud_drift_drifted_feature_count",
        "Number of features flagged as drifted",
        registry=registry,
    ).set(len(report.drifted_features))
    if report.score_psi is not None:
        Gauge(
            "fraud_drift_score_psi",
            "PSI of the prediction score distribution",
            registry=registry,
        ).set(report.score_psi)
    if report.score_ks_p_value is not None:
        Gauge(
            "fraud_drift_score_ks_p_value",
            "KS test p-value for the prediction score distribution",
            registry=registry,
        ).set(report.score_ks_p_value)

    push_to_gateway(
        settings.pushgateway_url, job=f"fraud_drift_{report.model_name}", registry=registry
    )
