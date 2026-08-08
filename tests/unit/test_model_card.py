import pytest

from src.ml.model_card import render_model_card

pytestmark = pytest.mark.unit


@pytest.fixture
def sample_card_kwargs():
    return dict(
        model_name="fraud_xgboost",
        model_version="3",
        algorithm="XGBoost (gradient-boosted trees)",
        imbalance_technique="scale_pos_weight=11.5 (class weighting)",
        training_params={"n_estimators": 200, "max_depth": 5},
        metrics={"precision": 0.53, "recall": 0.81, "f1": 0.64, "roc_auc": 0.96},
        fairness_report={
            "US": {"count": 100, "recall": 0.8, "precision": 0.5, "positive_rate": 0.1},
            "GB": {"count": 80, "recall": 0.78, "precision": 0.49, "positive_rate": 0.11},
        },
        dataset_summary={
            "description": "Synthetic data",
            "total_rows": 1000,
            "positive_rate": 0.08,
            "train_test_split": "75/25",
            "num_cardholders": 200,
            "num_features": 20,
        },
        limitations=["Synthetic training labels.", "No real fairness audit."],
    )


class TestRenderModelCard:
    def test_includes_model_name_and_version(self, sample_card_kwargs):
        card = render_model_card(**sample_card_kwargs)
        assert "fraud_xgboost" in card
        assert "v3" in card

    def test_includes_all_metrics(self, sample_card_kwargs):
        card = render_model_card(**sample_card_kwargs)
        assert "0.5300" in card  # precision
        assert "0.8100" in card  # recall

    def test_includes_fairness_groups(self, sample_card_kwargs):
        card = render_model_card(**sample_card_kwargs)
        assert "| US |" in card
        assert "| GB |" in card

    def test_includes_all_limitations(self, sample_card_kwargs):
        card = render_model_card(**sample_card_kwargs)
        assert "Synthetic training labels." in card
        assert "No real fairness audit." in card

    def test_includes_governance_section(self, sample_card_kwargs):
        card = render_model_card(**sample_card_kwargs)
        assert "Governance" in card
        assert "model_approvals" in card

    def test_includes_imbalance_technique(self, sample_card_kwargs):
        card = render_model_card(**sample_card_kwargs)
        assert "scale_pos_weight=11.5" in card
