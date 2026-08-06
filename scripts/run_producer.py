#!/usr/bin/env python3
"""CLI entrypoint for the Kafka transaction producer. Also starts a
Prometheus metrics server so `docker-compose`'s Prometheus can scrape it."""
import click

from src.common.config import get_settings
from src.ingestion.producer import run
from src.monitoring.metrics import start_metrics_server


@click.command()
@click.option("--tps", default=None, type=int, help="Transactions per second to simulate.")
@click.option("--edge-case-ratio", default=None, type=float, help="Fraction of edge-case transactions.")
def main(tps: int | None, edge_case_ratio: float | None) -> None:
    settings = get_settings()
    start_metrics_server(settings.prometheus_port)
    run(tps=tps, edge_case_ratio=edge_case_ratio)


if __name__ == "__main__":
    main()
