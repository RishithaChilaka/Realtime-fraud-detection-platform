"""
Train XGBoost and LightGBM fraud classifiers, track both with MLflow, and
register+stage the resulting model versions.

Class imbalance handling (fraud is rare, so a naive model just predicts
"not fraud" and gets high accuracy while catching nothing):
  - **XGBoost** is trained with `scale_pos_weight` set to the train-set
    negative:positive ratio -- XGBoost's native class-weighting mechanism.
  - **LightGBM** is trained on a SMOTE-oversampled training set
    (imbalanced-learn) -- synthetic minority-class examples rather than
    reweighting, to demonstrate/compare both standard techniques.

Both models are evaluated on the same untouched, non-resampled test split,
so their metrics are directly comparable and reflect real-world class
balance.
"""
from __future__ import annotations

from typing import Any

import lightgbm as lgb
import mlflow
import numpy as np
import pandas as pd
import xgboost as xgb
from imblearn.over_sampling import SMOTE
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

from src.common.config import Settings, get_settings
from src.common.logging_config import configure_logging
from src.ml import registry
from src.ml.dataset import build_training_dataset
from src.ml.model_card import render_model_card
from src.ml.model_features import FEATURE_COLUMNS, select_model_matrix

logger = configure_logging("ml_train")


def _evaluate(y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5) -> dict[str, float]:
    y_pred = (y_prob >= threshold).astype(int)
    return {
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "roc_auc": float(roc_auc_score(y_true, y_prob)) if len(set(y_true)) > 1 else 0.0,
        "pr_auc": float(average_precision_score(y_true, y_prob)) if len(set(y_true)) > 1 else 0.0,
        "positive_rate_predicted": float(y_pred.mean()),
        "positive_rate_actual": float(y_true.mean()),
    }


def _fairness_report(
    df_test: pd.DataFrame, y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5
) -> dict[str, dict[str, float]]:
    """Recall/precision/positive-rate broken out by cardholder home country
    -- the closest proxy this synthetic dataset has to a protected
    attribute. A model with wildly different recall across countries would
    be flagged here before promotion."""
    y_pred = (y_prob >= threshold).astype(int)
    report: dict[str, dict[str, float]] = {}
    for country in sorted(df_test["country"].unique()):
        mask = (df_test["country"] == country).to_numpy()
        if mask.sum() == 0:
            continue
        yt, yp = y_true[mask], y_pred[mask]
        report[country] = {
            "count": float(mask.sum()),
            "recall": float(recall_score(yt, yp, zero_division=0)),
            "precision": float(precision_score(yt, yp, zero_division=0)),
            "positive_rate": float(yp.mean()),
        }
    return report


def _log_confusion_matrix(y_true: np.ndarray, y_prob: np.ndarray, threshold: float, artifact_name: str) -> None:
    y_pred = (y_prob >= threshold).astype(int)
    cm = confusion_matrix(y_true, y_pred, labels=[0, 1])
    text = (
        "Confusion matrix (rows=actual, cols=predicted, labels=[legit, fraud])\n"
        f"{cm.tolist()}\n"
    )
    mlflow.log_text(text, artifact_name)


def _prepare_split(df: pd.DataFrame, test_size: float = 0.25, seed: int = 42):
    X = select_model_matrix(df)
    y = df["label"].to_numpy()
    X_train, X_test, y_train, y_test, df_train, df_test = train_test_split(
        X, y, df, test_size=test_size, random_state=seed, stratify=y
    )
    return X_train, X_test, y_train, y_test, df_train, df_test


def train_xgboost(
    df: pd.DataFrame, settings: Settings, dataset_summary: dict[str, Any]
) -> tuple[str, str]:
    """Train + log + register the XGBoost model. Returns (run_id, version)."""
    X_train, X_test, y_train, y_test, _, df_test = _prepare_split(df)

    neg, pos = int((y_train == 0).sum()), int((y_train == 1).sum())
    scale_pos_weight = neg / max(pos, 1)

    params = {
        "n_estimators": 200,
        "max_depth": 5,
        "learning_rate": 0.08,
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "scale_pos_weight": scale_pos_weight,
        "objective": "binary:logistic",
        "eval_metric": "aucpr",
        "random_state": 42,
    }

    with mlflow.start_run(run_name="xgboost_fraud") as run:
        model = xgb.XGBClassifier(**params)
        model.fit(X_train, y_train)

        y_prob = model.predict_proba(X_test)[:, 1]
        metrics = _evaluate(y_test, y_prob)
        fairness = _fairness_report(df_test, y_test, y_prob)

        mlflow.log_params(params)
        mlflow.log_param("imbalance_technique", "scale_pos_weight (class weighting)")
        mlflow.log_metrics(metrics)
        for country, stats in fairness.items():
            for k, v in stats.items():
                mlflow.log_metric(f"fairness_{country}_{k}", v)
        _log_confusion_matrix(y_test, y_prob, 0.5, "confusion_matrix_xgboost.txt")

        mlflow.xgboost.log_model(model, artifact_path="model")

        card = render_model_card(
            model_name=settings.mlflow_xgboost_model_name,
            model_version="pending",
            algorithm="XGBoost (gradient-boosted trees)",
            imbalance_technique=(
                f"scale_pos_weight={scale_pos_weight:.2f} (class weighting; "
                f"train-set was {neg} legitimate vs {pos} fraud examples)"
            ),
            training_params=params,
            metrics=metrics,
            fairness_report=fairness,
            dataset_summary=dataset_summary,
            limitations=_shared_limitations(),
        )
        mlflow.log_text(card, "model_card.md")

        logger.info("xgboost_trained", run_id=run.info.run_id, **metrics)

    version = registry.register_and_stage(
        settings, run.info.run_id, settings.mlflow_xgboost_model_name, "model", target_stage="Staging"
    )
    _write_model_card_file(settings.mlflow_xgboost_model_name, version.version, card)
    return run.info.run_id, version.version


def train_lightgbm(
    df: pd.DataFrame, settings: Settings, dataset_summary: dict[str, Any]
) -> tuple[str, str]:
    """Train + log + register the LightGBM model, using SMOTE to rebalance
    the training set. Returns (run_id, version)."""
    X_train, X_test, y_train, y_test, _, df_test = _prepare_split(df)

    pre_smote_counts = {"legit": int((y_train == 0).sum()), "fraud": int((y_train == 1).sum())}
    smote = SMOTE(random_state=42)
    X_train_res, y_train_res = smote.fit_resample(X_train, y_train)
    post_smote_counts = {
        "legit": int((y_train_res == 0).sum()),
        "fraud": int((y_train_res == 1).sum()),
    }

    params = {
        "n_estimators": 300,
        "max_depth": -1,
        "num_leaves": 31,
        "learning_rate": 0.05,
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "objective": "binary",
        "random_state": 42,
    }

    with mlflow.start_run(run_name="lightgbm_fraud") as run:
        model = lgb.LGBMClassifier(**params)
        model.fit(X_train_res, y_train_res)

        y_prob = model.predict_proba(X_test)[:, 1]
        metrics = _evaluate(y_test, y_prob)
        fairness = _fairness_report(df_test, y_test, y_prob)

        mlflow.log_params(params)
        mlflow.log_param("imbalance_technique", "SMOTE oversampling")
        mlflow.log_param("smote_pre_counts", str(pre_smote_counts))
        mlflow.log_param("smote_post_counts", str(post_smote_counts))
        mlflow.log_metrics(metrics)
        for country, stats in fairness.items():
            for k, v in stats.items():
                mlflow.log_metric(f"fairness_{country}_{k}", v)
        _log_confusion_matrix(y_test, y_prob, 0.5, "confusion_matrix_lightgbm.txt")

        mlflow.lightgbm.log_model(model, artifact_path="model")

        card = render_model_card(
            model_name=settings.mlflow_lightgbm_model_name,
            model_version="pending",
            algorithm="LightGBM (gradient-boosted trees)",
            imbalance_technique=(
                f"SMOTE oversampling (train set rebalanced from "
                f"{pre_smote_counts} to {post_smote_counts})"
            ),
            training_params=params,
            metrics=metrics,
            fairness_report=fairness,
            dataset_summary=dataset_summary,
            limitations=_shared_limitations(),
        )
        mlflow.log_text(card, "model_card.md")

        logger.info("lightgbm_trained", run_id=run.info.run_id, **metrics)

    version = registry.register_and_stage(
        settings, run.info.run_id, settings.mlflow_lightgbm_model_name, "model", target_stage="Staging"
    )
    _write_model_card_file(settings.mlflow_lightgbm_model_name, version.version, card)
    return run.info.run_id, version.version


def _shared_limitations() -> list[str]:
    return [
        "Training labels come from a synthetic transaction simulator's injected "
        "edge cases, not analyst-confirmed real-world fraud -- expect distribution "
        "shift when this model is retrained on real labeled data from the analyst "
        "feedback loop (`analyst_feedback` table).",
        "Fairness evaluation uses cardholder home country as a proxy slice because "
        "no demographic attributes are collected; it does not substitute for a "
        "full fairness audit against protected classes.",
        "The model has not been evaluated for robustness to adversarial/adaptive "
        "fraud patterns that specifically target its known feature set.",
        "Class imbalance handling (SMOTE / scale_pos_weight) was tuned for this "
        "synthetic dataset's ~8% injected fraud rate; real-world fraud rates are "
        "typically far lower (well under 1%) and would need re-tuning.",
    ]


def _write_model_card_file(model_name: str, version: str, card_markdown: str) -> None:
    import pathlib

    out_dir = pathlib.Path(__file__).resolve().parents[2] / "model_cards"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{model_name}_v{version}.md"
    path.write_text(card_markdown.replace("(v{model_version})", f"(v{version})"))
    logger.info("model_card_written", path=str(path))


def run(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment(settings.mlflow_experiment_name)

    logger.info("building_training_dataset")
    df = build_training_dataset()
    dataset_summary = {
        "description": (
            "Synthetic transactions from `TransactionGenerator` (Phase 1), with "
            "rolling-window features computed by `features.compute_features` in "
            "true per-card chronological order, identical to the live pipeline."
        ),
        "total_rows": len(df),
        "positive_rate": round(float(df["label"].mean()), 4),
        "train_test_split": "75% train / 25% test, stratified on label",
        "num_cardholders": df["card_id"].nunique(),
        "num_features": len(FEATURE_COLUMNS),
    }
    logger.info("dataset_built", **{k: v for k, v in dataset_summary.items() if k != "description"})

    xgb_run_id, xgb_version = train_xgboost(df, settings, dataset_summary)
    lgb_run_id, lgb_version = train_lightgbm(df, settings, dataset_summary)

    return {
        "xgboost": {"run_id": xgb_run_id, "version": xgb_version, "stage": "Staging"},
        "lightgbm": {"run_id": lgb_run_id, "version": lgb_version, "stage": "Staging"},
        "dataset_summary": dataset_summary,
    }


if __name__ == "__main__":
    result = run()
    print(result)
