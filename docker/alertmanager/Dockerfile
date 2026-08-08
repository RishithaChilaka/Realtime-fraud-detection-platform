# Bakes alertmanager.yml into the image instead of bind-mounting it at
# runtime -- see docker/postgres/Dockerfile for why.
FROM prom/alertmanager:v0.27.0

COPY alertmanager/alertmanager.yml /etc/alertmanager/alertmanager.yml
