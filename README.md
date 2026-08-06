# Real-Time Fraud Detection Platform

Production-grade platform for real-time credit-card fraud detection, built in phases.

- **Phase 1 — Core Infrastructure & Streaming Pipeline**: ingestion, schema validation,
  real-time feature engineering, storage, observability.
- **Phase 2 — ML Model, Real-Time Inference & Explainability**: XGBoost/LightGBM
  training with MLflow tracking and a governed model registry, a FastAPI scoring
  service with SHAP explanations and rule-based fallback, a Postgres-backed
  prediction audit trail and analyst review queue, and a Streamlit human-in-the-loop
  review UI.

## Architecture

```
                 ┌─────────────┐        ┌────────────────────┐
  TransactionGenerator          │  Kafka  │      Spark Structured Streaming     │
  (edge cases: high-value, ---> │ topic:  │ ---> validate (Pydantic)            │
   impossible travel,           │ transac-│ ---> compute rolling-window features│
   velocity abuse)              │ tions   │      (1h/24h counts, avg amount,    │
                 └─────────────┘  .raw   │       velocity, geo-distance)       │
                                          └───────────┬───────────┬────────────┘
                                                       │           │
                                            ┌──────────▼──┐   ┌────▼─────────┐
                                            │  PostgreSQL │   │ Redis feature│
                                            │  (system of │   │ store (<10ms │
                                            │  record +   │   │ p95 serving) │
                                            │  audit log) │   │              │
                                            └─────────────┘   └──────────────┘

  Prometheus scrapes producer + consumer metrics ---> Grafana dashboard
```

### Phase 2 additions

```
  Transaction JSON --> FastAPI /score --------> ModelState (loaded once at startup)
                            │                       │
                            │                       ├── Production model from MLflow
                            │                       │   (mlflow.xgboost/lightgbm.load_model)
                            │                       │
                            │                       └── RuleBasedFallback (velocity,
                            │                            amount z-score, impossible travel)
                            │                            used when no model is available
                            ▼
                     decide(score) -> risk_level, decision, routed_to_review
                            │
              ┌─────────────┼─────────────────────┐
              ▼             ▼                      ▼
        predictions    review_cases          (approve: no further action)
        (audit trail)  (analyst queue)
                            │
                            ▼
                  Streamlit review UI --> POST /review/{id}/feedback --> analyst_feedback
                                                                          (retraining data)

  MLflow: train.py logs XGBoost (scale_pos_weight) and LightGBM (SMOTE) runs,
  registers both as new "Staging" versions. Promotion to "Production" requires
  scripts/promote_model.py, which writes a model_approvals audit row BEFORE
  calling MLflow's transition_model_version_stage -- see src/ml/registry.py.
```

Design principles (SOLID):
- **Single Responsibility** — `ingestion/`, `feature_engineering/`, `storage/`,
  `monitoring/` are separate packages; each module does one job.
- **Dependency Inversion** — services depend on `Settings` (src/common/config.py)
  and thin client wrappers (`RedisClient`, `PostgresClient`), never on raw env vars
  or global connections, so everything is mockable in tests.
- **Open/Closed** — `features.compute_features` is pure and returns a typed
  `FeatureVector`; new features are added by extending that function and its unit
  tests, without touching the Spark job's I/O/orchestration code.
- **Single source of truth for the data contract** — `src/common/schemas.py` defines
  `Transaction` (Pydantic, used by the producer) and `spark_transaction_schema`
  (Spark StructType, used by the consumer) side by side, so a field change forces
  both to be updated together.

## Project layout

```
src/
  common/              # config, structured logging, shared Transaction schema
  ingestion/           # transaction simulator + Kafka producer
  feature_engineering/ # pure feature logic, validation, Spark job, Redis feature store
  storage/              # PostgreSQL (SQLAlchemy) + Redis client wrappers
  monitoring/           # Prometheus metric definitions
  ml/                   # (Phase 2) dataset builder, feature vectorization, training,
                         # MLflow registry + approval gate, model card renderer
  api/                  # (Phase 2) FastAPI service: routes, inference, SHAP, fallback,
                         # decision policy
streamlit_app/          # (Phase 2) analyst review UI
scripts/                # CLI entrypoints (producer, consumer, train_models, promote_model, ...)
docker/                 # Dockerfiles: producer, consumer, api, training, streamlit
sql/init.sql            # PostgreSQL schema (mirrors postgres_models.py)
model_cards/            # governance docs, generated by src/ml/train.py per model version
loadtest/                # Locust load test + results summarizer + report
prometheus/, grafana/   # monitoring stack config + dashboard
tests/unit/             # pure-logic tests, no external services required
tests/integration/      # Kafka/Spark/Postgres and FastAPI /score+/explain, via testcontainers
.github/workflows/ci.yml
```

## Quickstart

```bash
cp .env.example .env
docker compose up -d --build
```

This starts Kafka (KRaft mode, no Zookeeper), Spark master/worker, PostgreSQL, Redis,
MLflow, the FastAPI inference service, the Streamlit review UI, Prometheus, Grafana,
and the producer/consumer application containers.

- Spark UI: http://localhost:8080 &nbsp;·&nbsp; MLflow UI: http://localhost:5000
- Fraud API docs (Swagger): http://localhost:8080/docs &nbsp;·&nbsp; health: `/health`
- Analyst review UI: http://localhost:8501
- Prometheus: http://localhost:9090 &nbsp;·&nbsp; Grafana: http://localhost:3000 (admin/admin)
- Producer/consumer/API metrics: `:8001/metrics`, `:8002/metrics`, `:8080/metrics`

Train and register models (one-off job, not started by `up` by default):

```bash
docker compose --profile training run --rm training
```

This logs two runs to MLflow (`fraud_xgboost`, `fraud_lightgbm`), registers both as
new `Staging` versions, and writes a model card to `model_cards/`. Promote one to
`Production` (the API serves whichever model `MLFLOW_ACTIVE_MODEL_NAME` points at,
default `fraud_xgboost`):

```bash
python scripts/promote_model.py --model-name fraud_xgboost --version 1 \
    --approved-by "your_name" --notes "meets recall/precision bar on held-out set"
curl -X POST http://localhost:8080/admin/reload-model  # picks up the new Production version
```

Tear down: `docker compose down -v`

## Running locally without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python scripts/run_producer.py --tps 200 --edge-case-ratio 0.05
python scripts/run_consumer.py            # requires Java + Spark; see docker/consumer/Dockerfile
python scripts/train_models.py            # requires a running MLflow server (MLFLOW_TRACKING_URI)
uvicorn src.api.main:app --reload --port 8080
streamlit run streamlit_app/review_ui.py
```

## Testing

```bash
pytest tests/unit -v -m unit                 # fast, no external services
pytest tests/integration -v -m integration   # requires Docker (testcontainers)
make lint                                    # flake8 + black --check + isort --check-only
```

Phase 2's integration tests (`tests/integration/test_api_endpoints.py`) train a real,
small XGBoost model inline and inject it in place of an MLflow-served one, so
`/score` and `/explain` (including the actual `shap.TreeExplainer` path) are tested
against real PostgreSQL and Redis containers without needing a running MLflow server.

## Transaction edge cases simulated

`TransactionGenerator` (src/ingestion/transaction_generator.py) builds a population of
synthetic cardholders with realistic baseline spending, then injects:

- **high_value** — amount 15-40 standard deviations above the cardholder's norm
- **impossible_travel** — two transactions for the same card, thousands of km apart,
  minutes apart (physically implausible implied speed)
- **velocity_abuse** — 6-15 transactions for the same card within seconds of each other
- **new_device_high_value** — large amount from a device never seen for that card

`edge_case_ratio` (default 5%) controls injection frequency; every injected event is
labeled `is_simulated_fraud=True` for offline evaluation — this label is never exposed
to the real-time feature/scoring path.

## Feature store

`RedisFeatureStore` (src/feature_engineering/feature_store.py) is a minimal, Feast-
compatible surface (`get_features`/`write_features`) backed directly by Redis:
- `features:card:{card_id}` — latest computed `FeatureVector`, JSON, TTL'd
- `history:card:{card_id}` — bounded (500 entries) list of recent raw transactions,
  used to compute correct rolling windows across Spark micro-batch boundaries

## Success criteria — status

### Phase 1

| Criterion | Status |
|---|---|
| Modular SOLID project structure | Done |
| Docker Compose: Kafka, Spark, PostgreSQL, Redis, Prometheus, Grafana | Done |
| Kafka producer with realistic + edge-case transactions | Done |
| Spark Structured Streaming: schema validation, rolling-window features, feature store | Done |
| PostgreSQL persistence + audit log | Done |
| Redis feature caching / session state | Done |
| Unit tests for feature engineering (pytest) | Done — 40+ cases across features, schemas, feature store, generator, producer, validation |
| Integration tests: Kafka -> Spark logic -> PostgreSQL | Done (testcontainers; requires Docker) |
| GitHub Actions CI: lint, unit test, integration test, docker build | Done |
| 1K tx/sec at <200ms latency; feature store <10ms p95 | **Design target, not load-tested in this environment** |

### Phase 2

| Criterion | Status |
|---|---|
| XGBoost + LightGBM trained, with imbalance handling | Done — XGBoost: `scale_pos_weight`; LightGBM: SMOTE (`src/ml/train.py`) |
| MLflow experiment tracking + model registry with stage transitions | Done (`src/ml/registry.py`, `docker-compose.yml` mlflow service) |
| `/score` endpoint: fraud score + risk level | Done (`src/api/routes/score.py`) |
| `/explain` endpoint: SHAP top-5 contributions | Done (`shap.TreeExplainer`, `src/api/explain.py`) |
| Confidence-threshold routing to review queue | Done (`src/api/decision.py`) |
| Rule-based fallback when ML unavailable | Done (`src/api/fallback.py` — velocity, amount z-score, impossible travel) |
| PostgreSQL: prediction audit trail + review case queue | Done (`predictions`, `review_cases` tables) |
| Human-in-the-loop UI + feedback -> retraining data | Done (Streamlit `streamlit_app/review_ui.py` -> `analyst_feedback` table) |
| Model cards (purpose, data, metrics, fairness, limitations) | Done — auto-generated by `train.py`; see `model_cards/` (currently illustrative examples, see note) |
| Approval workflow gating Production promotion | Done (`model_approvals` table is a hard precondition in `src/ml/registry.py::promote_model`) |
| Integration tests for `/score`/`/explain` | Done (`tests/integration/test_api_endpoints.py`, real trained XGBoost + testcontainers) |
| Load test report: 500 RPS at <100ms p95 | **Tooling built, not run against a live API in this environment** — see `loadtest/load_test_report.md` |

### Transparency note: what was and wasn't actually executed

Both phases were built in a sandboxed environment with **no Docker daemon and no
network access to PyPI/package registries** (`pip install` fails with a
proxy/allowlist error here). That means:

- **Verified in this environment**: every Python file compiles (`py_compile`) and
  every YAML/JSON config parses; the pure-logic pieces with zero third-party
  dependencies (`src/ml/model_card.py`, `loadtest/summarize_results.py`) were
  actually executed here to confirm their output, and the model cards in
  `model_cards/*_vEXAMPLE-1.md` and the load test report's example table are real
  output from those runs against representative sample data.
- **Not verified in this environment** (requires Docker + installable
  dependencies, neither available here): `docker compose up`, `pytest` end to end
  (xgboost/mlflow/fastapi/etc. aren't installed), the actual training run, and the
  real load test against a live API.

The GitHub Actions CI pipeline (`.github/workflows/ci.yml`) runs all of this for
real on every push — lint, unit tests, integration tests (including the trained-model
SHAP path), and a Docker build matrix across all five service images. Before treating
either phase as production-ready, I'd run `docker compose up -d --build`,
`docker compose --profile training run --rm training`, and the real Locust command in
`loadtest/load_test_report.md` yourself and confirm the numbers.
