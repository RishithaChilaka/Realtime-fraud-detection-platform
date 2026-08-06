# Load Test Report: `/score` and `/explain`

## Target (Phase 2 success criteria)

- 500 requests/sec sustained against `/score`
- p95 latency < 100ms
- SHAP explanations (`/explain`) render in < 50ms (measured separately as
  `fraud_shap_compute_latency_seconds` in Prometheus -- the SHAP compute
  step alone, not the full HTTP round trip which also includes feature
  lookup and the model call)

## How to run it for real

This sandbox has no Docker daemon and no network access to install
`fastapi`/`xgboost`/`locust`/etc., so the numbers below are **not from a
real run against a live API** -- see the "About this report" note. To
produce a real report:

```bash
docker compose up -d --build
# wait for `curl http://localhost:8090/health` to return 200

pip install -r requirements-dev.txt
mkdir -p loadtest/results
locust -f loadtest/locustfile.py --host http://localhost:8090 \
    --users 500 --spawn-rate 50 --run-time 3m --headless \
    --csv loadtest/results/run1

python loadtest/summarize_results.py loadtest/results/run1_stats.csv
```

Watch `Requests/s` in Locust's own output to confirm you actually reached
~500 RPS (it's a function of user count, wait time, and how fast the
server responds -- not something `--users 500` guarantees by itself).
Cross-check against Grafana's `fraud_score_latency_seconds` panel, which
measures server-side handler latency (feature lookup + model + Postgres
audit write) independent of network/Locust overhead.

## About this report

`loadtest/summarize_results.py` is a real, working tool (pure Python
stdlib `csv`, no dependencies) -- it was executed against a synthetic
`stats.csv` shaped exactly like Locust's real output format, to confirm
the parsing and pass/fail logic are correct. The table below is that
synthetic example, **not a measurement of the actual API**. Delete this
section and replace the table with your own `run1_stats.csv` output once
you've run the command above against a live stack.

### Example output (synthetic, for tooling verification only)

| Endpoint | Requests | Failures | RPS | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|
| /score | 81000 | 12 | 540.3 | 38 | 88 | 140 |
| /explain | 9000 | 4 | 60.1 | 44 | 95 | 150 |
| /health | 9000 | 0 | 60.0 | 3 | 8 | 12 |
| **Aggregated** | 99000 | 16 | 600.4 | 38 | **89** | 142 |

**Result** (synthetic example): p95=89ms (PASS vs <100ms target),
throughput=600.4 req/s (PASS vs 500 req/s target).

## Design choices made to hit the real targets

These are architectural decisions already reflected in the code, worth
checking against your real results if they come in slower than target:

- **Model and SHAP explainer built once at startup**, not per request
  (`src/api/inference.py::ModelState`) -- the expensive parts (loading
  from MLflow, constructing `shap.TreeExplainer`) happen exactly once per
  process, not per call.
- **`shap.TreeExplainer`**, not model-agnostic SHAP (`KernelExplainer`) --
  exact Shapley values for tree ensembles in time roughly linear in
  (trees x depth), which is what makes single-row explanation latency
  low enough to plausibly hit <50ms.
- **Feature computation reads, not writes, the Redis feature store**
  (`RedisFeatureStore.get_history`) -- `/score` never blocks on a Postgres
  or Kafka write before responding; the prediction audit write happens
  after the score is computed and returned is prepared, not before.
- **Connection pooling**: `RedisClient` uses a shared, process-level
  `redis.ConnectionPool`; `PostgresClient` uses SQLAlchemy's pooled engine
  -- both created once at startup, not per request.

## Known risk areas if the real numbers don't hit target

- PostgreSQL synchronous writes inside the `/score` request path
  (`pg_client.write_prediction`, and `create_review_case` for routed
  cases) are the most likely p95 tail-latency contributor under load --
  the audit trail requirement makes them hard to remove, but they could
  move to an async/background write (accepting a small durability window)
  if latency numbers demand it.
- `mlflow.xgboost.load_model`/`mlflow.lightgbm.load_model` at startup can
  be slow depending on the MLflow artifact backend (local filesystem here,
  see docker-compose.yml) -- this affects cold-start time, not steady
  state p95, but is worth knowing about for rolling deploys.
