from unittest.mock import MagicMock, patch

import pytest

from src.common.config import Settings
from src.ml.registry import ApprovalRequiredError, promote_model

pytestmark = pytest.mark.unit


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def mock_mlflow_client():
    client = MagicMock()
    client.get_model_version.return_value = MagicMock(current_stage="Staging")
    with patch("src.ml.registry._client", return_value=client):
        yield client


@pytest.fixture
def mock_pg_client():
    pg = MagicMock()
    pg.has_approval.return_value = True
    return pg


class TestPromoteModel:
    def test_promotion_writes_approval_before_transitioning_stage(
        self, mock_mlflow_client, mock_pg_client, settings
    ):
        promote_model(
            settings=settings,
            model_name="fraud_xgboost",
            model_version="3",
            approved_by="alice",
            notes="looks good",
            pg_client=mock_pg_client,
        )

        mock_pg_client.record_model_approval.assert_called_once()
        call_kwargs = mock_pg_client.record_model_approval.call_args.kwargs
        assert call_kwargs["model_name"] == "fraud_xgboost"
        assert call_kwargs["model_version"] == "3"
        assert call_kwargs["approved_by"] == "alice"
        assert call_kwargs["to_stage"] == "Production"

        mock_mlflow_client.transition_model_version_stage.assert_called_once_with(
            name="fraud_xgboost", version="3", stage="Production", archive_existing_versions=True
        )

    def test_promotion_without_recorded_approval_raises(self, mock_mlflow_client, mock_pg_client, settings):
        mock_pg_client.has_approval.return_value = False

        with pytest.raises(ApprovalRequiredError):
            promote_model(
                settings=settings,
                model_name="fraud_xgboost",
                model_version="3",
                approved_by="alice",
                pg_client=mock_pg_client,
            )

        mock_mlflow_client.transition_model_version_stage.assert_not_called()

    def test_promotion_from_non_staging_stage_still_proceeds_with_warning(
        self, mock_mlflow_client, mock_pg_client, settings
    ):
        mock_mlflow_client.get_model_version.return_value = MagicMock(current_stage="Archived")

        promote_model(
            settings=settings,
            model_name="fraud_xgboost",
            model_version="3",
            approved_by="alice",
            pg_client=mock_pg_client,
        )

        mock_mlflow_client.transition_model_version_stage.assert_called_once()
