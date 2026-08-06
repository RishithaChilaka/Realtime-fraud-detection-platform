# Real-Time Fraud Detection Platform — Phase 1: Core Infrastructure & Streaming Pipeline

Production-grade streaming foundation for a real-time credit-card fraud detection
system. Phase 1 covers ingestion, validation, real-time feature engineering, storage,
and observability. Model training/serving and the fraud-scoring API come in later
phases.

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
scripts/                # CLI entrypoints (run_producer, run_consumer, wait_for_services)
docker/                 # Dockerfiles for producer & consumer images
sql/init.sql            # PostgreSQL schema (mirrors postgres_models.py)
prometheus/, grafana/   # monitoring stack config + dashboard
tests/unit/             # pure-logic tests, no external services required
tests/integration/      # Kafka -> Spark logic -> PostgreSQL, via testcontainers
.github/workflows/ci.yml
```

## Quickstart

```bash
cp .env.example .env
docker compose up -d --build
```

This starts Kafka (KRaft mode, no Zookeeper), Spark master/worker, PostgreSQL, Redis,
Prometheus, Grafana, and the producer/consumer application containers.

- Spark UI: http://localhost:8080
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin/admin, dashboard auto-provisioned)
- Producer/consumer metrics: http://localhost:8001/metrics, http://localhost:8002/metrics

Tear down: `docker compose down -v`

## Running locally without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python scripts/run_producer.py --tps 200 --edge-case-ratio 0.05
python scripts/run_consumer.py   # requires Java + Spark; see docker/consumer/Dockerfile base image
```

## Testing

```bash
pytest tests/unit -v -m unit                 # fast, no external services
pytest tests/integration -v -m integration   # requires Docker (testcontainers)
make lint                                    # flake8 + black --check + isort --check-only
```

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
| 1K tx/sec at <200ms latency; feature store <10ms p95 | **Design target, not yet load-tested** — see note below |

### Note on the 1K tx/sec / <200ms / <10ms success criteria

This code was written and statically verified (all modules import-clean, all configs
YAML/JSON-valid, logic manually traced against the test suite) in a sandboxed
environment with no Docker daemon and no package-registry network access, so the
full stack has not actually been run end-to-end or load-tested here. The `Makefile`
and CI pipeline give you the exact commands to do that once you have Docker locally:

```bash
docker compose up -d --build
python scripts/run_producer.py --tps 1000
# watch Prometheus/Grafana for fraud_stream_batch_latency_seconds and
# fraud_feature_store_read_latency_seconds against the targets
```

I'd flag this as the one thing worth confirming yourself before calling Phase 1 done.
