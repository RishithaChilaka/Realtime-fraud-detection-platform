# bitnami/spark was pulled from Docker Hub's free tier in Aug 2025; pinned
# tags moved to the frozen bitnamilegacy/* namespace (see docker-compose.yml).
FROM bitnamilegacy/spark:3.5.1

USER root
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY scripts ./scripts

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "scripts/wait_for_services.py"]
CMD ["python3", "scripts/run_consumer.py"]
