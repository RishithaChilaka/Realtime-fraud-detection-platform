# Airflow runs from the official image + a thin requirements overlay,
# deliberately kept separate from the api/training/consumer images -- see
# requirements.txt's note on why apache-airflow isn't bundled everywhere,
# and docker/airflow/requirements.txt for why the reverse (this image
# doesn't carry fastapi/shap/streamlit) is also true.
FROM apache/airflow:2.8.4-python3.10

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

USER airflow
COPY docker/airflow/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# The DAGs import `src.*` (feature engineering, ML training, drift
# detection, storage) directly -- mount/copy the app package so
# `sys.path.insert(0, "/opt/airflow/app")` in each DAG resolves it.
COPY --chown=airflow:airflow src /opt/airflow/app/src
