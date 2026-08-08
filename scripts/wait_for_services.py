#!/usr/bin/env python3
"""Blocks until Kafka, PostgreSQL, and Redis are reachable, then execs into
the real command. Used as the ENTRYPOINT for the api/producer/consumer/
training containers (each Dockerfile's CMD -- e.g. `uvicorn ...` or
`python scripts/run_producer.py`) so they don't crash-loop while
`docker compose` is still starting dependent services.

Docker only appends a Dockerfile's CMD as extra argv to the ENTRYPOINT
process (sys.argv[1:] here) -- it does NOT run CMD as a separate step.
This script is responsible for actually launching those argv itself once
it's safe to do so; os.execvp replaces this process with that command
(instead of spawning a child), so it keeps PID 1 and correctly receives
signals like SIGTERM from `docker compose down`/`stop`. Without the
os.execvp call at the end, this script would just print its readiness
checks and exit 0 -- and the container would exit right along with it,
having never started the actual service."""
import os
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

    command = sys.argv[1:]
    if not command:
        raise RuntimeError(
            "wait_for_services.py was run with no command to exec -- it's meant to be "
            "the ENTRYPOINT with the real command as CMD (e.g. ['uvicorn', ...]), not "
            "run standalone."
        )
    os.execvp(command[0], command)


if __name__ == "__main__":
    try:
        main()
    except TimeoutError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
