# Bakes provisioning config + dashboard JSON into the image instead of
# bind-mounting them at runtime -- see docker/postgres/Dockerfile for why.
# grafana/grafana runs as the non-root "grafana" user (uid 472); --chown
# matches the ownership pattern the base image already uses for
# /etc/grafana and /var/lib/grafana so the app can read these at startup.
FROM grafana/grafana:10.4.2

COPY --chown=472:472 grafana/provisioning /etc/grafana/provisioning
COPY --chown=472:472 grafana/dashboards /var/lib/grafana/dashboards
