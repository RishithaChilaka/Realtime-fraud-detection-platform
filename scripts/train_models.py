#!/usr/bin/env python3
"""CLI entrypoint: build the training dataset, train XGBoost + LightGBM,
log everything to MLflow, and register both as new `Staging` versions."""
from src.common.config import get_settings
from src.ml.train import run

if __name__ == "__main__":
    settings = get_settings()
    result = run(settings)
    print("Training complete:")
    for model_key in ("xgboost", "lightgbm"):
        info = result[model_key]
        print(f"  {model_key}: run_id={info['run_id']} version={info['version']} stage={info['stage']}")
    print(
        "\nBoth models are in Staging. Promote one to Production with:\n"
        "  python scripts/promote_model.py --model-name fraud_xgboost "
        f"--version {result['xgboost']['version']} --approved-by <you> --notes '...'"
    )
