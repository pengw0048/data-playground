# syntax=docker/dockerfile:1
# Data Playground — one image: build the SPA, then serve SPA + API + engine in one process.
# See docker-compose.yml for a Postgres-backed, restart-durable setup, and the README "Scaling out"
# section for running several stateless web instances behind a load balancer.

# --- 1. build the SPA (Vite) ---
# Base images pinned to a digest (not a mutable :tag) so a rebuild is reproducible and can't silently
# pull a changed base — resolve a new digest with `docker buildx imagetools inspect <img>:<tag>` (OPS-10).
FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build          # → /web/dist

# --- 2. kernel + the bundled SPA ---
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS app
# Release identity (REL-01 / issue #114): bake package version + build SHA so /api/version never
# reports "unknown" from a published image, and so `docker inspect` can read the OCI version label.
ARG DP_VERSION=0.2.2
ARG DP_GIT_SHA=unknown
LABEL org.opencontainers.image.title="Data Playground" \
      org.opencontainers.image.version="${DP_VERSION}" \
      org.opencontainers.image.revision="${DP_GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/pengw0048/data-playground"
ENV DP_GIT_SHA=${DP_GIT_SHA}

# Pin uv so the installer itself can't drift under a rebuild (OPS-02).
RUN pip install --no-cache-dir uv==0.11.28
WORKDIR /app
COPY kernel/ ./kernel/
# the SPA at ../web/dist relative to kernel/ — where pyproject's force-include bundles it into
# hub/_web at build time (so the served SPA matches the packaged kernel)
COPY --from=web /web/dist ./web/dist
WORKDIR /app/kernel
# --frozen: install EXACTLY the checked-in uv.lock (fail if drifted) → reproducible. --no-dev: no test
# tooling in the runtime image. pod+postgres extras = k8s client + psycopg for the deployed topology.
RUN uv sync --frozen --no-dev --extra pod --extra postgres

# Put the uv venv on PATH so a bare `python` / `dataplay` resolves to it. REQUIRED for the pod kernel
# substrate: PodSpawner launches a kernel pod with `python -m hub.kernel …`, and without this that bare
# `python` is the system interpreter, which can't import `hub` (it lives in /app/kernel/.venv).
ENV PATH="/app/kernel/.venv/bin:$PATH"
# Unbuffered stdout/stderr so a container's (and a kernel pod's) logs stream live to `kubectl logs`
# instead of sitting in Python's block buffer until the process exits.
ENV PYTHONUNBUFFERED=1

ENV DP_WORKSPACE=/data
# The single-image build binds 0.0.0.0 in OPEN mode (no auth), which the CLI otherwise refuses. This
# opts in explicitly: it's a SINGLE-USER image — trust the network/firewall, or set DP_AUTH_SECRET
# (as docker-compose.yml does) for multi-user auth. A loud warning prints at startup in open mode.
ENV DP_ALLOW_INSECURE_BIND=1
EXPOSE 8471
VOLUME ["/data"]

# Run as a non-root user (OPS-10): defence in depth — canvas code runs as this user, not root. /data is
# created + owned here so the mounted named volume initializes writable; a host bind-mount's ownership
# is the operator's to set. The venv + app tree are chowned so `uv run` works without root.
RUN useradd --create-home --uid 10001 dp \
    && mkdir -p /data \
    && chown -R dp:dp /app /data
USER dp

# --no-open: no browser in a container. Bind all interfaces; workspace (canvases/outputs/plugins +
# the SQLite fallback DB) + seeded sample data live on the mounted /data volume. Point DP_DATABASE_URL
# at Postgres (see docker-compose.yml) to make the metadata DB shared + the web tier stateless.
CMD ["uv", "run", "dataplay", "--host", "0.0.0.0", "--port", "8471", "--no-open", "--workspace", "/data"]
