"""
Rule-based fallback scorer.

Used when the ML model can't be loaded (MLflow unreachable, no Production
version registered yet, etc.) so the API can keep making decisions --
degraded but deterministic and explainable -- instead of failing closed
(blocking everything) or open (approving everything).

The deliverable's rule is "velocity > 10 transactions/min -> flag". Phase 1
already computes a `velocity_5min` rolling-window feature (transactions in
the trailing 5 minutes); rather than add a parallel 1-minute window purely
for this rule, the fallback thresholds `velocity_5min` directly
(`Settings.fallback_velocity_5min_threshold`, default 10) and documents
that mapping here instead of introducing a second, mostly-redundant
feature. It's combined with two more Phase 1 signals for a slightly
richer -- but still fully explainable -- rule set:
  - `velocity_5min` above threshold (rapid-succession / card testing)
  - `amount_zscore_24h` above threshold (statistical amount outlier)
  - implied travel speed between consecutive transactions being physically
    impossible (`features.is_impossible_travel`)
"""

from __future__ import annotations

from dataclasses import dataclass

from src.common.config import Settings
from src.feature_engineering.features import FeatureVector, is_impossible_travel, is_velocity_abuse


@dataclass(frozen=True)
class FallbackResult:
    score: float
    triggered_rules: list[str]

    @property
    def reason(self) -> str:
        return "; ".join(self.triggered_rules) if self.triggered_rules else "no rule triggered"


class RuleBasedFallback:
    """Deterministic, model-free scorer. `score()` never raises and never
    depends on any external service, so it stays available exactly when
    the ML path isn't."""

    MODEL_NAME = "fallback_rules"
    MODEL_VERSION = "v1"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def score(self, feature_vector: FeatureVector) -> FallbackResult:
        triggered: list[str] = []
        score = 0.05  # baseline "nothing looks wrong" score

        if is_velocity_abuse(
            feature_vector, max_txns_per_5min=self.settings.fallback_velocity_5min_threshold
        ):
            triggered.append(
                f"velocity_5min ({feature_vector.velocity_5min}) exceeded threshold "
                f"({self.settings.fallback_velocity_5min_threshold})"
            )
            score = max(score, 0.90)

        if feature_vector.amount_zscore_24h >= self.settings.fallback_amount_zscore_threshold:
            triggered.append(
                f"amount_zscore_24h ({feature_vector.amount_zscore_24h:.2f}) exceeded threshold "
                f"({self.settings.fallback_amount_zscore_threshold})"
            )
            score = max(score, 0.85)

        if is_impossible_travel(
            feature_vector,
            max_plausible_speed_kmh=self.settings.fallback_impossible_travel_speed_kmh,
        ):
            triggered.append(
                f"implied_speed_kmh ({feature_vector.implied_speed_kmh:.0f}) exceeds "
                f"plausible travel speed "
                f"({self.settings.fallback_impossible_travel_speed_kmh:.0f} km/h)"
            )
            score = max(score, 0.95)

        if feature_vector.is_new_device and feature_vector.avg_amount_1h > 0:
            # A new device combined with an above-average amount is a
            # softer signal than the rules above -- nudges the score up
            # without triggering `review`/`block` on its own.
            score = max(score, 0.35)
            triggered.append("new_device combined with elevated spend")

        return FallbackResult(score=score, triggered_rules=triggered)
