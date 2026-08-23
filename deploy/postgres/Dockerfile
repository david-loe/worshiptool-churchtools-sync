# syntax=docker/dockerfile:1.7
FROM postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2

COPY --chmod=0555 entrypoint.sh /opt/worshipsync/postgres-entrypoint.sh
COPY --chmod=0555 001-create-app-role.sh /docker-entrypoint-initdb.d/001-create-app-role.sh

ENTRYPOINT ["/bin/sh", "/opt/worshipsync/postgres-entrypoint.sh"]
CMD ["postgres"]
