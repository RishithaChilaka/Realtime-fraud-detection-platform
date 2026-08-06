"""
Centralized application configuration.

All environment-dependent settings live here so that other modules never
read `os.environ` directly. This keeps configuration a single, testable
seam (Dependency Inversion: modules depend on `Settings`, not on the
environment).
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Kafka
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_transactions: str = "transactions.raw"
    kafka_topic_dlq: str = "transactions.dlq"

    # Spark
    spark_master_url: str = "local[*]"
    spark_checkpoint_dir: str = "/tmp/checkpoints/transactions"
    spark_app_name: str = "fraud-detection-streaming"

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "fraud_detection"
    postgres_user: str = "fraud_admin"
    postgres_password: str = "change_me_in_prod"
    # "prefer" locally (docker-compose Postgres has no TLS listener);
    # RDS's parameter group forces "require" server-side regardless (see
    # terraform/modules/rds's `rds.force_ssl=1`), but setting this to
    # "require" client-side too makes the client fail fast/clearly instead
    # of relying solely on server enforcement.
    postgres_sslmode: str = "prefer"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    feature_ttl_seconds: int = 86400
    # Empty string = no AUTH (matches docker-compose's unauthenticated
    # local Redis). ElastiCache requires both of these to be set -- see
    # terraform/modules/elasticache's `auth_token`/`transit_encryption_mode`.
    redis_auth_token: str = ""
    redis_use_tls: bool = False

    # Producer
    producer_tps: int = 200
    producer_edge_case_ratio: float = 0.05

    # Monitoring
    prometheus_port: int = 8000

    # --- Phase 2: MLflow ---
    mlflow_tracking_uri: str = "http://localhost:5000"
    mlflow_experiment_name: str = "fraud-detection"
    mlflow_xgboost_model_name: str = "fraud_xgboost"
    mlflow_lightgbm_model_name: str = "fraud_lightgbm"
    mlflow_active_model_name: str = "fraud_xgboost"  # which registered model the API serves

    # --- Phase 2: scoring / risk thresholds ---
    # A score >= high_risk_threshold is blocked outright; a score below
    # low_risk_threshold is auto-approved. Everything in between, plus any
    # score inside the "uncertain band", is routed to analyst review.
    risk_high_threshold: float = 0.80
    risk_low_threshold: float = 0.20
    review_confidence_band_low: float = 0.40
    review_confidence_band_high: float = 0.60

    # --- Phase 2: rule-based fallback (used when the ML model is unavailable) ---
    fallback_velocity_5min_threshold: int = 10
    fallback_amount_zscore_threshold: float = 5.0
    fallback_impossible_travel_speed_kmh: float = 900.0

    # --- Phase 2: FastAPI service ---
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # --- Phase 3: MLOps / observability ---
    pushgateway_url: str = "localhost:9091"
    drift_ks_p_value_threshold: float = 0.05
    drift_psi_threshold: float = 0.25
    business_metrics_window_hours: int = 24

    # --- Phase 3: automated promotion policy ---
    # Automated (no-human-in-the-loop) promotion is opt-in and, even when
    # enabled, still writes a normal `model_approvals` row (approved_by=
    # "airflow-automated-promotion") -- see src/ml/auto_promote.py. This
    # flag controls whether `dags/automated_promotion_dag.py` is allowed to
    # call it at all; default is off, because auto-promoting a fraud model
    # straight to Production is a real business risk most teams want a
    # human to explicitly opt into per-environment, not a hardcoded default.
    enable_automated_promotion: bool = False
    auto_promotion_min_recall: float = 0.70
    auto_promotion_min_precision: float = 0.30
    auto_promotion_max_fairness_recall_gap: float = 0.15

    # --- Phase 3: security ---
    jwt_secret_key: str = "change_me_in_prod_use_secrets_manager"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60
    use_aws_secrets_manager: bool = False
    aws_secrets_manager_secret_id: str = "fraud-detection/prod"
    aws_region: str = "us-east-1"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            f"?sslmode={self.postgres_sslmode}"
        )

    @property
    def postgres_jdbc_url(self) -> str:
        """JDBC URL for Spark's JDBC sink."""
        return (
            f"jdbc:postgresql://{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            f"?sslmode={self.postgres_sslmode}"
        )


@lru_cache
def get_settings() -> Settings:
    """Settings are cheap to build but env parsing should only happen once per process.

    When USE_AWS_SECRETS_MANAGER=true, the credential fields (postgres
    password, Redis auth token, JWT signing key) are overridden with
    values fetched from AWS Secrets Manager -- see src/common/secrets.py.
    That import is local (not top-level) to avoid a circular import:
    secrets.py itself depends on the `Settings` class defined above.
    """
    settings = Settings()
    if settings.use_aws_secrets_manager:
        from src.common.secrets import apply_aws_secrets

        settings = apply_aws_secrets(settings)
    return settings
