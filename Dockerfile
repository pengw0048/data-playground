# syntax=docker/dockerfile:1
# Data Playground — one image: build the SPA, then serve SPA + API + engine in one process.
# See docker-compose.yml for a Postgres-backed, restart-durable setup, and the README "Scaling out"
# section for running several stateless web instances behind a load balancer.

# --- 1. build the SPA (Vite) ---
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build          # → /web/dist

# --- 2. kernel + the bundled SPA ---
FROM python:3.12-slim AS app
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY kernel/ ./kernel/
# the SPA at ../web/dist relative to kernel/ — where pyproject's force-include bundles it into
# kernel/_web at build time (so the served SPA matches the packaged kernel)
COPY --from=web /web/dist ./web/dist
WORKDIR /app/kernel
RUN uv sync --extra postgres     # builds the package (force-includes ../web/dist) + runtime deps + psycopg

ENV DP_WORKSPACE=/data
# The single-image build binds 0.0.0.0 in OPEN mode (no auth), which the CLI otherwise refuses. This
# opts in explicitly: it's a SINGLE-USER image — trust the network/firewall, or set DP_AUTH_SECRET
# (as docker-compose.yml does) for multi-user auth. A loud warning prints at startup in open mode.
ENV DP_ALLOW_INSECURE_BIND=1
EXPOSE 8471
VOLUME ["/data"]

# --no-open: no browser in a container. Bind all interfaces; workspace (canvases/outputs/plugins +
# the SQLite fallback DB) + seeded sample data live on the mounted /data volume. Point DP_DATABASE_URL
# at Postgres (see docker-compose.yml) to make the metadata DB shared + the web tier stateless.
CMD ["uv", "run", "dataplay", "--host", "0.0.0.0", "--port", "8471", "--no-open", "--workspace", "/data"]
