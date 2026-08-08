# Real-Time Fraud Detection Platform

Production-grade platform for real-time credit-card fraud detection, built in phases.

- **Phase 1 — Core Infrastructure & Streaming Pipeline**: ingestion, schema validation,
  real-time feature engineering, storage, observability.
- **Phase 2 — ML Model, Real-Time Inference & Explainability**: XGBoost/LightGBM
  training with MLflow tracking and a governed model registry, a FastAPI scoring
  service with SHAP explanations and rule-based fallback, a Postgres-backed
  prediction audit trail and analyst review queue, and a Streamlit human-in-the-loop
  review UI.
- **Phase 3 — MLOps, Observability & Cloud Deployment**: Airflow DAGs for daily
  retraining (including analyst feedback) and statistical drift detection (KS test +
  PSI) with gated automated promotion, Prometheus/Grafana dashboards and Alertmanager
  rules across system/model/business metrics, JWT-based RBAC and PII-masked logging,
  and a full Terraform AWS deployment (ECS Fargate, RDS, ElastiCache, MSK) with a
  GitHub Actions CI/CD pipeline to ECR/ECS.
- **FraudShield upload dashboard**: a browser dashboard at `/dashboard/` -- upload a
  CSV of transactions, get back the same KPIs/charts/alerts table a live fraud team
  would see, scored by the exact same model and rules `/score` uses. See
  [Upload dashboard](#upload-dashboard-fraudshield) below.

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

### Phase 3 additions

```
  Airflow (dags/):
    retrain_pipeline_dag    -- daily: build_feedback_dataset (analyst_feedback +
                                synthetic) -> train XGBoost/LightGBM -> MLflow "Staging"
    drift_detection_dag     -- every 6h: KS test + PSI on features and prediction
                                scores vs. a reference window -> drift_reports table
                                + Pushgateway -> triggers automated_promotion_dag
    automated_promotion_dag -- gated on ENABLE_AUTOMATED_PROMOTION: validates the
                                Staging model (recall/precision/fairness thresholds)
                                and, if it passes AND drift was detected, promotes
                                it via the *same* model_approvals-gated
                                registry.promote_model() Phase 2 uses for human
                                approvals (approved_by="airflow-automated-promotion")
    business_metrics_dag    -- every 5m: block/review/approve rates, P50/P95 score,
                                review queue depth, analyst-confirmed FP/FN rate

  Prometheus (scrape + Pushgateway) --> Alertmanager (routes by `team` label:
    ml / sre / analysts) --> Grafana (system_overview / model_health / business_metrics
    dashboards, grafana/dashboards/*.json)

  src/api/auth.py: JWT + RBAC (service/analyst/admin) on /review/* and
    /admin/reload-model. src/common/pii.py: structlog processor masking
    card_id/ip_address/device_id/... on every log line platform-wide.

  terraform/environments/prod: VPC, ECR, RDS (KMS + TLS-forced), ElastiCache
    (encrypted + AUTH token), MSK (TLS + KMS), Secrets Manager, IAM (incl. GitHub
    OIDC deploy role), ECS Fargate + ALB (host-based routing, ACM/HTTPS)
  .github/workflows/deploy.yml: on CI success on main, builds+pushes images to
    ECR (SHA-tagged) and rolls each ECS service via a new task definition revision
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
  ml/drift.py, ml/auto_promote.py   # (Phase 3) PSI/KS drift detection, gated auto-promotion
  monitoring/business_metrics.py    # (Phase 3) block/review/FP-FN rate exporter (Pushgateway)
  api/auth.py, api/middleware.py    # (Phase 3) JWT/RBAC, request metrics + access log
  common/pii.py, common/secrets.py  # (Phase 3) log PII masking, AWS Secrets Manager loader
  api/batch.py, api/routes/batch.py # upload dashboard: CSV parsing + in-memory batch scoring
frontend/                # upload-a-CSV FraudShield dashboard (served by the API at /dashboard/)
streamlit_app/          # (Phase 2) analyst review UI, (Phase 3) login flow
scripts/                # CLI entrypoints (producer, consumer, train_models, promote_model, ...)
docker/                 # Dockerfiles: producer, consumer, api, training, streamlit, airflow
sql/init.sql            # PostgreSQL schema (mirrors postgres_models.py)
model_cards/            # governance docs, generated by src/ml/train.py per model version
loadtest/                # Locust load test + results summarizer + report
dags/                   # (Phase 3) Airflow TaskFlow DAGs: retrain, drift, auto-promote, business metrics
prometheus/, grafana/   # monitoring stack config + dashboards (alert_rules.yml is Phase 3)
alertmanager/           # (Phase 3) alert routing by team (ml / sre / analysts)
terraform/              # (Phase 3) AWS deployment: VPC/ECR/RDS/ElastiCache/MSK/IAM/ECS -- see terraform/README.md
tests/unit/             # pure-logic tests, no external services required
tests/integration/      # Kafka/Spark/Postgres and FastAPI /score+/explain, via testcontainers
.github/workflows/ci.yml        # lint, unit+integration tests, docker build matrix
.github/workflows/deploy.yml    # (Phase 3) build+push to ECR, roll ECS services
```

## Quickstart

```bash
cp .env.example .env
docker compose up -d --build
```

This starts Kafka (KRaft mode, no Zookeeper), Spark master/worker, PostgreSQL, Redis,
MLflow, the FastAPI inference service, the Streamlit review UI, Prometheus, Grafana,
Alertmanager, Pushgateway, kafka-exporter, Airflow (webserver + scheduler, its own
metadata Postgres), and the producer/consumer application containers -- 20 services
total (`docker compose config --services` to list them all).

- Spark UI: http://localhost:8080 &nbsp;·&nbsp; MLflow UI: http://localhost:5001 (host port 5001, not 5000 -- macOS's AirPlay Receiver claims 5000 by default)
- Fraud API docs (Swagger): http://localhost:8090/docs &nbsp;·&nbsp; health: `/health`
  (host port 8090, not 8080 -- Spark's UI already owns 8080; the container itself
  still listens on 8080 internally, so `FRAUD_API_BASE_URL=http://api:8080` inside
  the compose network is unaffected)
- Analyst review UI: http://localhost:8501 (login with one of the demo accounts in
  `src/api/auth.py`'s `_DEMO_USERS`, e.g. `analyst1` / `analyst-demo-pass`)
- FraudShield upload dashboard: http://localhost:8090/dashboard/ -- see below
- Prometheus: http://localhost:9090 &nbsp;·&nbsp; Grafana: http://localhost:3000 (admin/admin)
  &nbsp;·&nbsp; Alertmanager: http://localhost:9093 &nbsp;·&nbsp; Airflow: http://localhost:8793
  (default `admin`/`admin`, set by `docker/airflow`'s init step)
- Producer/consumer/API metrics: `:8001/metrics`, `:8002/metrics`, `:8090/metrics`

### First-time setup checklist (get to a fully working local stack)

1. `cp .env.example .env` and `docker compose up -d --build` (above).
2. Wait for `curl http://localhost:8090/health` to return `200` (the API depends on
   MLflow/Postgres/Redis being healthy first; can take ~30-60s on first boot).
3. Train and register a model (see below) so `/score` serves real predictions instead
   of the rule-based fallback: `docker compose --profile training run --rm training`,
   then `python scripts/promote_model.py ...` and `POST /admin/reload-model`.
4. In the Airflow UI (http://localhost:8793), unpause `retrain_pipeline_dag`,
   `drift_detection_dag`, `automated_promotion_dag`, and `business_metrics_dag` --
   DAGs are paused by default on first deploy, matching Airflow's own default.
5. Open Grafana (http://localhost:3000) -- the four dashboards under
   `grafana/dashboards/` (`pipeline_overview`, `api_slo`, `model_health`,
   `business_metrics`) are auto-provisioned; no manual import needed.

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

## Upload dashboard (FraudShield)

A browser dashboard at `/dashboard/` (served by the same FastAPI process as
`/score` -- no separate frontend container) for the "I have a file of
transactions, show me what the fraud model thinks of it" workflow, styled
after a real-time SOC-style dashboard mockup: sidebar nav, KPI cards, an
alerts-over-time chart, a world map of flagged transactions, a fraud-reason
breakdown, a flagged-transactions table, and a model performance panel.

**How it works**: click "Download sample CSV" to get a realistic file
(generated with the same `TransactionGenerator` the Kafka producer uses, via
`GET /batch/template`), or upload your own with the columns listed on the
page (`transaction_id, card_id, user_id, amount, merchant_id,
merchant_category, transaction_type, channel, latitude, longitude, country,
event_time`, plus optional `currency`, `device_id`, `ip_address`). The upload
posts to `POST /batch/score`, which:

1. Validates every row against the same `Transaction` schema the Kafka
   producer/consumer use, skipping (and reporting) malformed rows rather
   than failing the whole file.
2. Sorts transactions by `event_time` and replays them through the *exact*
   feature engineering (`compute_features`), model (or rule-based fallback),
   and routing logic `/score` uses -- see `src/api/batch.py`. The one
   difference from live scoring: rolling-window history (velocity, amount
   z-score, impossible travel) is built **in memory from the uploaded file
   itself**, not read from the live Redis feature store, and nothing is
   written to Postgres or Redis. An upload is a one-off "what would the
   platform have done with this data" analysis, not live traffic -- it won't
   show up in the Streamlit review queue or the prediction audit trail.
3. Returns the aggregated KPIs, a timeseries bucketed across the file's own
   `event_time` range (there's no "now" for an uploaded file), a fraud-reason
   breakdown built from the same rule primitives `RuleBasedFallback` uses
   (velocity/amount-outlier/impossible-travel/new-device, plus "model signal"
   for rows the ML model flagged that no simple rule would have caught), the
   flagged transactions themselves, and the current model's **last validated
   training-run metrics** (precision/recall/F1/AUC-ROC from MLflow) --
   labeled as such, not computed from the upload, since an unlabeled file has
   no ground truth to score against.

This is deliberately additive, not a replacement for the Streamlit analyst
review UI, which does a different job (individual case review + feedback
submission that feeds retraining) -- see `src/api/batch.py`'s module
docstring for the full reasoning.

## Cloud deployment (AWS)

Full instructions, prerequisites, and the "what wasn't actually run" caveat are in
[`terraform/README.md`](terraform/README.md). Short version:

```bash
cd terraform/environments/prod
cp terraform.tfvars.example terraform.tfvars   # set domain_name, acm_certificate_arn, ...
terraform init && terraform plan && terraform apply
```

This provisions a VPC, ECR repos, RDS Postgres (x2: app + Airflow metadata), ElastiCache
Redis, MSK (Kafka), Secrets Manager, IAM (including a GitHub OIDC deploy role), and an
ECS Fargate cluster behind an ALB with host-based routing (`api.<domain>`,
`review.<domain>`, `grafana.<domain>`, `mlflow.<domain>`, `airflow.<domain>`). After
`apply`, set the repo variables `terraform/README.md` lists (`AWS_DEPLOY_ROLE_ARN`,
`ECS_CLUSTER_NAME`, ...) and every merge to `main` that passes CI auto-deploys via
`.github/workflows/deploy.yml`.

## Monitoring guide

- **Grafana** (`:3000` locally, `grafana.<domain>` on AWS) has four auto-provisioned
  dashboards: `pipeline_overview` (Kafka/Spark/producer throughput — Phase 1),
  `api_slo` (request rate, p50/p95/p99 latency, error rate by status class),
  `model_health` (prediction score distribution, drift PSI/KS, fallback-active
  status), `business_metrics` (block/review/approve rate, review queue depth,
  analyst-confirmed FP/FN rate).
- **Prometheus** (`:9090`) scrapes the producer/consumer/API directly and
  `kafka-exporter`; batch jobs (drift reports, business metrics) push through
  **Pushgateway** (`:9091`) since they run on a schedule, not continuously — see
  `src/ml/drift.py::push_drift_metrics` and `src/monitoring/business_metrics.py::push_snapshot`.
- **Alertmanager** (`:9093`) routes every rule in `prometheus/alert_rules.yml` by its
  `team` label (`ml` / `sre` / `analysts`) to a receiver in `alertmanager/alertmanager.yml`.
  Receivers are generic webhook placeholders out of the box (valid, runnable, not wired
  to a real Slack/PagerDuty) — swap in `slack_configs`/`email_configs` to actually notify
  someone; on AWS those credentials would come from Secrets Manager.
- **Airflow** (`:8793`) is where `drift_detection_dag`, `retrain_pipeline_dag`,
  `automated_promotion_dag`, and `business_metrics_dag` run and log; check a DAG's
  task logs there before anything else if a Prometheus/Grafana metric it feeds looks
  frozen or stale.

## Runbook

**Kafka consumer lag** (`KafkaConsumerLagHigh` alert, `kafka_consumergroup_lag_sum`
on Grafana's `pipeline_overview` dashboard):
1. Open the Spark UI (`:8080` locally) and check the Structured Streaming job for
   stalled or repeatedly-failing micro-batches — a lagging-but-healthy job just needs
   time or more `spark-worker` capacity; a failing one needs the batch error in the
   Spark UI's logs.
2. Confirm Kafka itself is healthy (`docker compose logs kafka`) — a broker issue
   looks identical to a slow consumer from the lag metric alone.
3. If throughput is the bottleneck, `docker compose up -d --scale spark-worker=N`
   locally, or bump `consumer`'s Fargate `cpu`/`memory` in
   `terraform/environments/prod/main.tf`'s `services.consumer` block on AWS (recall
   the consumer runs Spark in `local[*]` mode there — see that file's comment on
   why it isn't a real clustered deployment).

**Model drift** (`HighModelDrift`/`HighModelDriftSustained` alert,
`fraud_drift_any_detected`/`fraud_drift_max_psi` on `model_health`):
1. Check `drift_detection_dag`'s latest run in Airflow, or query the `drift_reports`
   table directly for `drifted_features` and `max_psi`.
2. If `ENABLE_AUTOMATED_PROMOTION=true`, `automated_promotion_dag` will already have
   attempted a promotion — check `model_approvals` for a row with
   `approved_by='airflow-automated-promotion'` and the model card in `model_cards/`
   for the validated metrics.
3. If automated promotion is off, or ran but failed its validation gate
   (`src/ml/auto_promote.py::evaluate_validation_gate` — recall/precision/fairness
   thresholds), review the latest Staging model's metrics in MLflow and promote
   manually with `scripts/promote_model.py` once satisfied, or trigger
   `retrain_pipeline_dag` manually first if the model hasn't retrained on recent
   feedback yet.

**High false positive rate** (`ElevatedFalsePositiveRate` alert,
`fraud_business_false_positive_rate` on `business_metrics`):
1. Open the Streamlit review UI (`:8501`) and filter recent analyst feedback for
   `confirmed_legitimate`/`false_positive` outcomes to see which transaction
   patterns are triggering false blocks.
2. Check whether `risk_high_threshold`/`risk_low_threshold`
   (`src/common/config.py`) need retuning, or whether the false positives cluster
   around a specific feature (e.g. `impossible_travel` false-triggering on
   legitimate frequent travelers) that suggests a feature engineering fix in
   `src/feature_engineering/features.py` rather than a threshold change.
3. If the false positives are recent and the model hasn't been retrained since they
   started, this analyst feedback is exactly what `retrain_pipeline_dag`'s
   `build_feedback_dataset` step will incorporate on its next daily run.

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

### Phase 3

| Criterion | Status |
|---|---|
| Airflow: daily retraining DAG including analyst feedback | Done — `dags/retrain_pipeline_dag.py`, `src/ml/dataset.py::build_feedback_dataset` |
| Airflow: drift detection (KS test + PSI) on features and scores | Done — `dags/drift_detection_dag.py`, `src/ml/drift.py` |
| Automated model promotion on drift + validation pass | Done — `dags/automated_promotion_dag.py`, `src/ml/auto_promote.py` (opt-in via `ENABLE_AUTOMATED_PROMOTION`; still writes a `model_approvals` audit row, same gate Phase 2's human approvals use) |
| Grafana dashboards: system, model, business metrics | Done — `grafana/dashboards/*.json`, auto-provisioned |
| Alertmanager rules: drift -> ML, latency -> SRE, queue -> analysts | Done — `prometheus/alert_rules.yml` + `alertmanager/alertmanager.yml` (routed by `team` label) |
| AWS deployment: Terraform, ECS/EKS, RDS, ElastiCache, MSK | Done — `terraform/` (ECS Fargate chosen over EKS; see `terraform/README.md`'s scope-simplification notes) |
| CI/CD: GitHub Actions -> ECR -> ECS | Done — `.github/workflows/deploy.yml`, GitHub OIDC (no static AWS keys) |
| Secrets management (Secrets Manager / Vault) | Done — `terraform/modules/secrets`, `src/common/secrets.py` |
| Security: encryption at rest/in transit, RBAC/JWT, PII masking | Done — RDS/ElastiCache/MSK/S3 KMS + forced TLS (Terraform), app-level `redis_use_tls`/`postgres_sslmode`, `src/api/auth.py` (JWT + RBAC), `src/common/pii.py` (log masking) |
| Comprehensive README: architecture, setup, API docs, monitoring, runbook | Done — this file + `terraform/README.md` |
| Real-time Grafana dashboards | **Design target — dashboards are provisioned and query real metric names, but were never viewed against a live Prometheus in this environment** |
| Alerts firing appropriately | **Design target — rules were reviewed for correct PromQL and one bug was caught and fixed (`HighAPIErrorRate` originally referenced a metric/label that could never be true; see git history), but no alert has actually fired in a real Alertmanager in this environment** |
| Full AWS deployment with public endpoints | **Not deployed — no AWS account in this environment; Terraform is statically reviewed only (see `terraform/README.md`)** |
| README enabling <30 minute local setup | Done, with the caveat that "<30 minutes" assumes a machine with Docker already installed and enough resources to run ~20 containers (Kafka, Spark, 2x Postgres, Redis, MLflow, Airflow, API, Streamlit, Prometheus stack) — see the first-time setup checklist above |

## Tech stack summary

| Layer | Technology |
|---|---|
| Streaming | Apache Kafka (KRaft), Spark Structured Streaming |
| Feature store | Redis (ElastiCache in AWS) |
| ML models | XGBoost (`scale_pos_weight`), LightGBM (SMOTE via imbalanced-learn) |
| Model lifecycle | MLflow tracking + registry, Airflow (retrain/drift/promote DAGs), model cards |
| APIs | FastAPI (`/score`, `/explain`, `/review`, `/auth`), JWT + RBAC |
| Databases | PostgreSQL (RDS in AWS): transactions, predictions, review cases, feedback, approvals, drift reports, audit log |
| Frontend | Streamlit (analyst review UI, login-gated) |
| Infrastructure | Docker Compose (local), Terraform -> AWS (VPC, ECS Fargate + ALB, RDS, ElastiCache, MSK, Secrets Manager, IAM/OIDC) |
| CI/CD | GitHub Actions: `ci.yml` (lint, unit+integration tests, docker build matrix), `deploy.yml` (build/push to ECR, roll ECS services) |
| Observability | Prometheus + Pushgateway, Grafana, Alertmanager, kafka-exporter |
| Testing | pytest (unit + testcontainers integration), Locust load testing |
| Governance | Model cards, `model_approvals` audit table (required for every Production promotion, human or automated), PII-masked structured logging |

### Transparency note: what was and wasn't actually executed

All three phases were built in a sandboxed environment with **no Docker daemon, no
network access to PyPI/package registries, no AWS account, and no Terraform/AWS CLI
binaries** (`pip install` fails with a proxy/allowlist error here). That means:

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
- **Phase 3 specifically**: every new Python file passes `py_compile`, every new
  YAML/JSON config (`docker-compose.yml`, `prometheus/*.yml`, `alertmanager.yml`,
  `grafana/dashboards/*.json`) parses with `yaml.safe_load`/`json.load`, and every
  `.tf` file was checked for brace/paren balance plus a full variable
  cross-reference (every `var.X` used is declared, every module call's arguments
  match its target module's variables, no undeclared/missing-required arguments).
  That review process is also what caught two real bugs before delivery: a
  Terraform security-group anti-pattern (inline `ingress` blocks mixed with
  standalone `aws_security_group_rule` resources on the same SG — a known-conflicting
  pattern) in `modules/rds`/`elasticache`/`msk`, and a docker-compose host port
  collision (both `spark-master` and `api` mapped to `8080:8080` — fixed by moving
  `api` to `8090:8080`). Neither `terraform plan`/`apply` nor a real Airflow
  scheduler nor a real Alertmanager has fired an alert in this environment —
  treat the AWS deployment and the DAG/alerting logic as reviewed-and-consistent,
  not as proven-in-production.

The GitHub Actions CI pipeline (`.github/workflows/ci.yml`) runs unit/integration
tests and a Docker build matrix for real on every push; `.github/workflows/deploy.yml`
runs for real too, but only against an actual AWS account with the repo variables in
`terraform/README.md` configured — neither has executed in this sandboxed session.
Before treating any phase as production-ready, I'd run `docker compose up -d --build`,
`docker compose --profile training run --rm training`, the real Locust command in
`loadtest/load_test_report.md`, and `terraform plan` against a real AWS account, and
confirm the numbers and the plan output yourself.
