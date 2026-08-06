#!/usr/bin/env python3
"""Blocks until Kafka, PostgreSQL, and Redis are reachable. Used by the
consumer/producer containers' entrypoint so they don't crash-loop while
`docker-compose` is still starting dependent services."""
import socket
import sys
import time

from src.common.config import get_settings


def _wait_for_tcp(host: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"OK: {host}:{port} is accepting connections")
                return
        except OSError as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError(f"Timed out waiting for {host}:{port}: {last_error}")


def main() -> None:
    settings = get_settings()
    kafka_host, kafka_port = settings.kafka_bootstrap_servers.split(":")
    targets = [
        (kafka_host, int(kafka_port)),
        (settings.postgres_host, settings.postgres_port),
        (settings.redis_host, settings.redis_port),
    ]
    for host, port in targets:
        _wait_for_tcp(host, port)


if __name__ == "__main__":
    try:
        main()
    except TimeoutError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
