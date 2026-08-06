"""Request/response contracts for the FastAPI inference service."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

RiskLevel = Literal["low", "medium", "high"]
Decision = Literal["approve", "review", "block"]
ModelSource = Literal["ml", "fallback_rules"]
ExplanationType = Literal["shap", "rule_based"]


class ScoreResponse(BaseModel):
    transaction_id: str
    card_id: str
    fraud_score: float = Field(..., ge=0, le=1)
    risk_level: RiskLevel
    decision: Decision
    model_source: ModelSource
    model_name: str
    model_version: str
    routed_to_review: bool
    review_case_id: Optional[str] = None
    latency_ms: float
    prediction_id: str


class FeatureContribution(BaseModel):
    feature: str
    value: float
    contribution: float


class ExplainResponse(BaseModel):
    transaction_id: str
    card_id: str
    explanation_type: ExplanationType
    fraud_score: float
    base_value: Optional[float] = None
    top_features: list[FeatureContribution]
    rule_based_reason: Optional[str] = None
    model_name: str
    model_version: str
    latency_ms: float


class ReviewCase(BaseModel):
    id: str
    prediction_id: str
    transaction_id: str
    fraud_score: float
    risk_level: RiskLevel
    reason: str
    status: str
    assigned_analyst: Optional[str] = None
    created_at: str


class FeedbackRequest(BaseModel):
    analyst_id: str = Field(..., min_length=1, max_length=64)
    label: Literal["confirmed_fraud", "confirmed_legitimate", "false_positive", "false_negative"]
    notes: Optional[str] = Field(default=None, max_length=1024)


class FeedbackResponse(BaseModel):
    feedback_id: str
    case_id: str
    status: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    model_loaded: bool
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    fallback_active: bool
