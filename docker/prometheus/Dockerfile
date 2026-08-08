# Bakes prometheus.yml + alert_rules.yml into the image instead of
# bind-mounting them at runtime -- see docker/postgres/Dockerfile for why.
FROM prom/prometheus:v2.51.2

COPY prometheus/prometheus.yml /etc/prometheus/prometheus.yml
COPY prometheus/alert_rules.yml /etc/prometheus/alert_rules.yml
