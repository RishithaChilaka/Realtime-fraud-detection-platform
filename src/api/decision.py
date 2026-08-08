"""
Pure score -> (risk_level, decision, routed_to_review) mapping.

Kept as one small, dependency-free function so the routing policy is
unit-testable without spinning up the API, a model, or a database, and so
`/score` and the Streamlit review UI can't disagree about what a given
score means.

Policy:
  - score >= risk_high_threshold           -> risk=high,   decision=block
  - score <= risk_low_threshold            -> risk=low,    decision=approve
  - otherwise                              -> risk=medium
  - ANY score inside the "uncertain band" (review_confidence_band_low..high)
    is routed to analyst review regardless of risk level, because the model
    itself is least confident there.
  - risk=high or risk=medium are also routed to review (a `block` decision
    still creates a case so an analyst can override a false positive).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.api.schemas import Decision, RiskLevel
from src.common.config import Settings


@dataclass(frozen=True)
class RoutingResult:
    risk_level: RiskLevel
    decision: Decision
    routed_to_review: bool
    reason: str


def decide(score: float, settings: Settings) -> RoutingResult:
    in_uncertain_band = (
        settings.review_confidence_band_low <= score <= settings.review_confidence_band_high
    )

    if score >= settings.risk_high_threshold:
        risk_level: RiskLevel = "high"
        decision: Decision = "block"
    elif score <= settings.risk_low_threshold and not in_uncertain_band:
        risk_level = "low"
        decision = "approve"
    else:
        risk_level = "medium"
        decision = "review"

    routed_to_review = decision != "approve"
    if in_uncertain_band:
        routed_to_review = True

    if in_uncertain_band:
        reason = f"score {score:.3f} is within the low-confidence review band"
    elif risk_level == "high":
        reason = f"score {score:.3f} at/above high-risk threshold ({settings.risk_high_threshold})"
    elif risk_level == "medium":
        reason = (
            f"score {score:.3f} between low ({settings.risk_low_threshold}) "
            "and high risk thresholds"
        )
    else:
        reason = f"score {score:.3f} at/below low-risk threshold ({settings.risk_low_threshold})"

    return RoutingResult(
        risk_level=risk_level, decision=decision, routed_to_review=routed_to_review, reason=reason
    )
