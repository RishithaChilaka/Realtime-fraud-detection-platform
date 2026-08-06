#!/usr/bin/env python3
"""CLI entrypoint for the Spark Structured Streaming consumer."""
from src.common.config import get_settings
from src.feature_engineering.spark_consumer import run
from src.monitoring.metrics import start_metrics_server

if __name__ == "__main__":
    settings = get_settings()
    start_metrics_server(settings.prometheus_port)
    run(settings)
