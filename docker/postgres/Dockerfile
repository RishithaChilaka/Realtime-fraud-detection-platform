# Bakes sql/init.sql into the image instead of bind-mounting it at runtime.
# Runtime bind mounts translate the host path through Docker Desktop's
# /host_mnt/... layer, which breaks if the host path contains a colon (e.g.
# a project folder named "ai:ml project" -- see docker-compose.yml's header
# note). Build-time COPY never goes through that translation, so it's
# unaffected regardless of what the host path looks like.
FROM postgres:16-alpine

COPY sql/init.sql /docker-entrypoint-initdb.d/init.sql
