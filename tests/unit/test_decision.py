import pytest

from src.api.decision import decide
from src.common.config import Settings

pytestmark = pytest.mark.unit


@pytest.fixture
def settings():
    return Settings(
        risk_high_threshold=0.80,
        risk_low_threshold=0.20,
        review_confidence_band_low=0.40,
        review_confidence_band_high=0.60,
    )


class TestDecide:
    def test_high_score_blocks(self, settings):
        result = decide(0.95, settings)
        assert result.risk_level == "high"
        assert result.decision == "block"
        assert result.routed_to_review is True

    def test_low_score_approves(self, settings):
        result = decide(0.05, settings)
        assert result.risk_level == "low"
        assert result.decision == "approve"
        assert result.routed_to_review is False

    def test_mid_score_routes_to_review(self, settings):
        result = decide(0.5, settings)
        assert result.risk_level == "medium"
        assert result.decision == "review"
        assert result.routed_to_review is True

    def test_score_in_uncertain_band_always_routed_even_if_technically_low(self, settings):
        # 0.20 (exactly risk_low_threshold) is also inside a hypothetical
        # tightened band; use a settings instance with an overlapping band
        # to prove the "uncertain band always wins" rule.
        s = Settings(
            risk_high_threshold=0.80,
            risk_low_threshold=0.30,
            review_confidence_band_low=0.10,
            review_confidence_band_high=0.35,
        )
        result = decide(0.20, s)
        assert result.routed_to_review is True

    def test_exactly_high_threshold_blocks(self, settings):
        result = decide(0.80, settings)
        assert result.decision == "block"

    def test_exactly_low_threshold_approves(self, settings):
        result = decide(0.20, settings)
        assert result.decision == "approve"

    def test_reason_is_human_readable(self, settings):
        result = decide(0.95, settings)
        assert "0.950" in result.reason
