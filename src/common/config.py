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

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    feature_ttl_seconds: int = 86400

    # Producer
    producer_tps: int = 200
    producer_edge_case_ratio: float = 0.05

    # Monitoring
    prometheus_port: int = 8000

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_jdbc_url(self) -> str:
        """JDBC URL for Spark's JDBC sink."""
        return f"jdbc:postgresql://{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"


@lru_cache
def get_settings() -> Settings:
    """Settings are cheap to build but env parsing should only happen once per process."""
    return Settings()
