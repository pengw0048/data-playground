# syntax=docker/dockerfile:1
# A Ray head/worker image for MULTI-NODE validation of the dp_ray distributed backend
# (docker-compose.ray.yml). It carries the `hub` package + the [ray] extra so every worker can run the
# shuffle compat shim (hub.ray_compat) and the map operators (hub.sandbox / hub.executors.engine).
#
# The [ray] extra is exactly pinned to the version whose private hash-shuffle ABI is validated and uses
# Ray's supported `data,default` dependency set. `default` is required by KubeRay's dashboard-agent
# /api/healthz probe on port 52365; a data-only install starts raylets but never serves that endpoint.
# The driver runs inside this image too, and dp_ray handshakes every live node before data execution.
#
# Base image pinned to a digest (not a mutable :tag) so a rebuild is reproducible and can't silently
# pull a changed base — resolve a new digest with `docker buildx imagetools inspect <img>:<tag>` (OPS-02).
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
# Pin uv so the installer itself can't drift under a rebuild (OPS-02).
RUN pip install --no-cache-dir uv==0.11.28
RUN groupadd --gid 10001 dataplay \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin dataplay
WORKDIR /app
RUN mkdir -p ./web/dist && chown -R 10001:10001 /app
COPY --chown=10001:10001 kernel/ ./kernel/
# the dp_ray reference backend the driver loads (+ its multi-node check script)
COPY --chown=10001:10001 examples/ ./examples/
# the wheel force-includes ../web/dist into hub/_web; a Ray node never serves the SPA, so a stub is enough
USER 10001:10001
RUN touch ./web/dist/.keep
WORKDIR /app/kernel
# --locked: install from the checked-in uv.lock and FAIL if pyproject/lock drifted; --no-dev: no test
# tooling. [ray] = ABI-pinned Ray validate_ray_cluster checks; [postgres] = hub-side client for the
# durable Ray Jobs acceptance gate (its production metadata store).
RUN uv sync --locked --no-dev --extra ray --extra postgres
# KubeRay starts raylets through a login shell, which resets PATH on Debian.
# Keep the venv-owned interpreter but expose the Ray CLI from the standard login-shell PATH.
USER root
RUN ln -s /app/kernel/.venv/bin/ray /usr/local/bin/ray
USER 10001:10001
ENV PATH="/app/kernel/.venv/bin:$PATH"
ENV HOME="/home/dataplay"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# workers use THIS interpreter (which has ray) instead of Ray re-launching them through uv
ENV RAY_ENABLE_UV_RUN_RUNTIME_ENV=0
# install the Ray 2.56.0 hash-shuffle compat shim on EVERY worker at startup — Ray Data's shuffle actors
# bypass the job runtime_env hook, but they inherit the raylet's env, and default_worker reads this var.
ENV __RAY_WORKER_PROCESS_SETUP_HOOK_ENV_VAR=hub.ray_compat.patch_hash_shuffle
EXPOSE 6379 8265 10001
