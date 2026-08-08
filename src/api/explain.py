"""
SHAP explainability for tree-model predictions.

Uses `shap.TreeExplainer`, which computes exact Shapley values for tree
ensembles in time roughly linear in (number of trees x tree depth) rather
than the exponential-in-feature-count cost of model-agnostic SHAP -- this
is what makes the <50ms target in the Phase 2 success criteria realistic
for a single-row explanation. The explainer is built once, at model-load
time (`ShapExplainer.__init__`), and reused across requests; building a
fresh explainer per request would dominate the latency budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import shap

from src.api.schemas import FeatureContribution
from src.ml.model_features import FEATURE_COLUMNS


@dataclass
class ShapExplanation:
    top_features: list[FeatureContribution]
    base_value: float
    latency_ms: float


class ShapExplainer:
    def __init__(self, model) -> None:
        # shap.TreeExplainer introspects the underlying booster; works
        # directly on XGBClassifier/LGBMClassifier sklearn wrappers.
        self._explainer = shap.TreeExplainer(model)

    def explain(self, row: pd.DataFrame, top_n: int = 5) -> ShapExplanation:
        start = time.perf_counter()
        raw = self._explainer.shap_values(row)

        # TreeExplainer returns a list [class0, class1] for some
        # binary-classifier configurations and a single array for others
        # (depends on model flavor/objective) -- normalize to "class 1
        # (fraud) contributions" either way.
        if isinstance(raw, list):
            shap_values = np.asarray(raw[1])[0]
            base_value = self._explainer.expected_value[1]
        else:
            shap_values = np.asarray(raw)[0]
            base_value = self._explainer.expected_value
            if isinstance(base_value, (list, np.ndarray)):
                base_value = base_value[-1]

        contributions = list(zip(FEATURE_COLUMNS, row.iloc[0].to_numpy(), shap_values))
        contributions.sort(key=lambda item: abs(item[2]), reverse=True)
        top = contributions[:top_n]

        latency_ms = (time.perf_counter() - start) * 1000
        return ShapExplanation(
            top_features=[
                FeatureContribution(feature=name, value=float(value), contribution=float(contrib))
                for name, value, contrib in top
            ],
            base_value=float(base_value),
            latency_ms=latency_ms,
        )
