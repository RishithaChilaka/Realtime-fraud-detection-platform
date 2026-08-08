"""
Build a labeled training dataset from the same simulator and feature logic
the live pipeline uses.

Using `TransactionGenerator` + `features.compute_features` for training
data (rather than a separate offline dataset generator) is deliberate: it
guarantees the *distribution* of feature values the model trains on is
exactly what the streaming/serving path will produce, and it means the
`is_simulated_fraud` label injected by the generator (high-value spikes,
impossible travel, velocity bursts, new-device+high-value -- see
src/ingestion/transaction_generator.py) doubles as ground truth for
supervised training.

This is obviously a simplification versus real historical transaction
data with real analyst-confirmed fraud labels; see the model card
("Known Limitations") for that caveat.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from src.common.logging_config import configure_logging
from src.feature_engineering.features import compute_features
from src.ingestion.transaction_generator import TransactionGenerator
from src.ml.model_features import FEATURE_COLUMNS, build_feature_row
from src.storage.postgres_client import PostgresClient

logger = configure_logging("ml_dataset")


def build_training_dataset(
    n_transactions: int = 40_000,
    num_cardholders: int = 800,
    edge_case_ratio: float = 0.08,
    seed: Optional[int] = 42,
) -> pd.DataFrame:
    """Generate `n_transactions` (approximately -- edge-case bursts can push
    the count slightly higher) and compute rolling-window features for each
    one, maintaining true per-card history exactly like the Spark consumer
    does, so 1h/24h windows and velocity features are realistic.

    Returns a DataFrame with all `model_features.FEATURE_COLUMNS` plus
    bookkeeping columns `transaction_id`, `card_id`, `country`, `event_time`,
    and the training label `label` (1 = simulated fraud, 0 = normal).
    """
    generator = TransactionGenerator(
        num_cardholders=num_cardholders, edge_case_ratio=edge_case_ratio, seed=seed
    )

    history: dict[str, list] = {}
    rows: list[dict] = []

    start_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
    for txn in generator.stream(n_transactions, start_time=start_time):
        card_history = history.setdefault(txn.card_id, [])
        feature_vector = compute_features(card_history, txn)

        row = build_feature_row(feature_vector, txn)
        row["transaction_id"] = txn.transaction_id
        row["card_id"] = txn.card_id
        row["country"] = txn.country
        row["event_time"] = txn.event_time
        row["label"] = int(txn.is_simulated_fraud)
        rows.append(row)

        card_history.append(txn)
        # Bound per-card history in memory during dataset construction, same
        # rationale as the Redis feature store's trim in Phase 1.
        if len(card_history) > 500:
            del card_history[: len(card_history) - 500]

    return pd.DataFrame(rows)


def build_feedback_dataset(
    pg_client: PostgresClient, lookback_days: int = 30, min_examples: int = 20
) -> Optional[pd.DataFrame]:
    """Analyst-labeled retraining examples from the human-in-the-loop
    feedback loop (Phase 2's `analyst_feedback` table), using the exact
    feature row captured at scoring time
    (`PostgresClient.fetch_labeled_training_examples`) so there's no
    train/serve skew between how these rows and the synthetic rows were
    featurized.

    Returns `None` (rather than an empty/tiny DataFrame) if fewer than
    `min_examples` labeled rows are available -- a handful of analyst
    labels blended into a 40k-row synthetic dataset would have negligible
    and unpredictable effect, so `train.run()` skips the merge entirely
    below that floor and logs why.
    """
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    examples = pg_client.fetch_labeled_training_examples(since)

    if len(examples) < min_examples:
        logger.info(
            "feedback_dataset_below_minimum",
            available=len(examples),
            required=min_examples,
            lookback_days=lookback_days,
        )
        return None

    rows = []
    for example in examples:
        features = example["input_features"]
        # Only keep rows whose feature schema still matches the current
        # model (protects against a stale row from before a feature-set
        # change silently corrupting the training matrix).
        if not all(col in features for col in FEATURE_COLUMNS):
            continue
        row = {col: features[col] for col in FEATURE_COLUMNS}
        row["label"] = example["label"]
        row["transaction_id"] = example["transaction_id"]
        row["country"] = features.get("country", "US")  # fairness slice; see note below
        rows.append(row)

    if len(rows) < min_examples:
        logger.info("feedback_dataset_below_minimum_after_schema_filter", available=len(rows))
        return None

    df = pd.DataFrame(rows)
    # `country` isn't part of FEATURE_COLUMNS (it's a fairness-slice-only
    # column, same as in build_training_dataset), and prediction audit rows
    # don't currently persist it -- default to "US" so the fairness report
    # in train.py doesn't crash on a missing column. Tracked as a known
    # limitation in the model card; see model_cards generation.
    logger.info("feedback_dataset_built", rows=len(df), lookback_days=lookback_days)
    return df
