# syntax=docker/dockerfile:1
# A Ray head/worker image for MULTI-NODE validation of the dp_ray distributed backend
# (docker-compose.ray.yml). It carries the `hub` package + the [ray] extra so every worker can run the
# shuffle compat shim (hub.ray_compat) and the map operators (hub.sandbox / hub.executors.engine).
#
# Ray is pinned to the version the operator was validated against locally, so the cluster behaves
# identically to `DP_TEST_RAY_LIVE=1` on a dev box. The driver runs INSIDE this image too (docker compose
# run driver …), so the Ray Client version always matches the cluster — no host↔cluster version coupling.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY kernel/ ./kernel/
# the dp_ray reference backend the driver loads (+ its multi-node check script)
COPY examples/ ./examples/
# the wheel force-includes ../web/dist into hub/_web; a Ray node never serves the SPA, so a stub is enough
RUN mkdir -p ./web/dist && touch ./web/dist/.keep
WORKDIR /app/kernel
RUN uv sync --extra ray \
    && uv pip install 'ray[data]==2.56.0'   # pin to the validated Ray version (matches the local differential)
ENV PATH="/app/kernel/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
# workers use THIS interpreter (which has ray) instead of Ray re-launching them through uv
ENV RAY_ENABLE_UV_RUN_RUNTIME_ENV=0
# install the Ray 2.56 hash-shuffle compat shim on EVERY worker at startup — Ray Data's shuffle actors
# bypass the job runtime_env hook, but they inherit the raylet's env, and default_worker reads this var.
ENV __RAY_WORKER_PROCESS_SETUP_HOOK_ENV_VAR=hub.ray_compat.patch_hash_shuffle
EXPOSE 6379 8265 10001
