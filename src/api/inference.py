"""
Application-lifetime model state and the core scoring routine.

`ModelState` is built once at FastAPI startup (see `main.py`'s lifespan
handler) and holds everything a request needs that's expensive to build:
the loaded MLflow model (or `None` if unavailable), its SHAP explainer,
the rule-based fallback, and shared clients. Building these per-request
would blow the latency budget; building them once and reusing them across
requests is what makes the <100ms p95 / <50ms SHAP targets achievable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.api.decision import RoutingResult, decide
from src.api.explain import ShapExplainer, ShapExplanation
from src.api.fallback import FallbackResult, RuleBasedFallback
from src.common.config import Settings
from src.common.logging_config import configure_logging
from src.common.schemas import Transaction
from src.feature_engineering.feature_store import RedisFeatureStore
from src.feature_engineering.features import FeatureVector, compute_features
from src.ml import registry
from src.ml.model_features import build_feature_row, rows_to_dataframe, select_model_matrix

logger = configure_logging("inference")


@dataclass
class ScoringResult:
    feature_vector: FeatureVector
    feature_row: pd.DataFrame
    score: float
    model_source: str  # "ml" | "fallback_rules"
    model_name: str
    model_version: str
    routing: RoutingResult
    latency_ms: float
    fallback_reason: Optional[str] = None


class ModelState:
    """Holds the currently loaded Production model (if any), its SHAP
    explainer, and the fallback scorer. `reload()` can be called to pick up
    a newly promoted model without restarting the process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = None
        self.explainer: Optional[ShapExplainer] = None
        self.model_name: Optional[str] = None
        self.model_version: Optional[str] = None
        self.run_id: Optional[str] = None
        self.fallback = RuleBasedFallback(settings)
        self.feature_store = RedisFeatureStore(settings)
        self.reload()

    def reload(self) -> bool:
        loaded = registry.load_production_model(self.settings)
        if loaded is None:
            logger.warning("model_unavailable_using_fallback")
            self.model = None
            self.explainer = None
            self.model_name = None
            self.model_version = None
            self.run_id = None
            return False

        self.model = loaded.model
        self.model_name = loaded.model_name
        self.model_version = loaded.model_version
        self.run_id = loaded.run_id
        try:
            self.explainer = ShapExplainer(loaded.model)
        except Exception as exc:  # pragma: no cover - defensive, SHAP is best-effort
            logger.error("shap_explainer_init_failed", error=str(exc))
            self.explainer = None
        logger.info("model_loaded", model_name=self.model_name, model_version=self.model_version)
        return True

    @property
    def is_model_available(self) -> bool:
        return self.model is not None


def build_feature_row_for(
    state: ModelState, txn: Transaction
) -> tuple[FeatureVector, pd.DataFrame]:
    history = state.feature_store.get_history(txn.card_id)
    feature_vector = compute_features(history, txn)
    row_dict = build_feature_row(feature_vector, txn)
    df = rows_to_dataframe([row_dict])
    return feature_vector, select_model_matrix(df)


def score_transaction(state: ModelState, txn: Transaction) -> ScoringResult:
    """Score one transaction end to end: compute features (read-only
    against the feature store -- see note in spark_consumer.py; the
    consumer, not this API, is responsible for appending confirmed
    transactions to history), score with the ML model if available,
    otherwise fall back to rules, and apply the confidence-threshold
    routing policy."""
    start = time.perf_counter()
    feature_vector, feature_row = build_feature_row_for(state, txn)

    fallback_reason = None
    if state.is_model_available:
        try:
            proba = state.model.predict_proba(feature_row)[:, 1]
            score = float(proba[0])
            model_source = "ml"
            model_name = state.model_name
            model_version = state.model_version
        except Exception as exc:  # model call failed at request time -> degrade gracefully
            logger.error("model_scoring_failed_falling_back", error=str(exc))
            fallback_result = state.fallback.score(feature_vector)
            score = fallback_result.score
            fallback_reason = fallback_result.reason
            model_source = "fallback_rules"
            model_name = state.fallback.MODEL_NAME
            model_version = state.fallback.MODEL_VERSION
    else:
        fallback_result: FallbackResult = state.fallback.score(feature_vector)
        score = fallback_result.score
        fallback_reason = fallback_result.reason
        model_source = "fallback_rules"
        model_name = state.fallback.MODEL_NAME
        model_version = state.fallback.MODEL_VERSION

    routing = decide(score, state.settings)
    latency_ms = (time.perf_counter() - start) * 1000

    return ScoringResult(
        feature_vector=feature_vector,
        feature_row=feature_row,
        score=score,
        model_source=model_source,
        model_name=model_name,
        model_version=model_version,
        routing=routing,
        latency_ms=latency_ms,
        fallback_reason=fallback_reason,
    )


def explain_transaction(
    state: ModelState, txn: Transaction
) -> tuple[ScoringResult, Optional[ShapExplanation]]:
    """Score + explain in one pass. Returns `(scoring_result, None)` when
    the transaction was scored by the rule-based fallback (SHAP only
    applies to the tree model); callers should fall back to
    `fallback_result.reason` as the explanation in that case."""
    scoring = score_transaction(state, txn)
    if scoring.model_source != "ml" or state.explainer is None:
        return scoring, None
    explanation = state.explainer.explain(scoring.feature_row)
    return scoring, explanation
