# syntax=docker/dockerfile:1.7
#
# danger_zone — multi-stage build
#
# Stage 1 (builder): install pinned deps from uv.lock into a venv
# Stage 2 (runtime): slim image with just the venv + source + tini for signals
#
# Build:   docker compose -f docker/compose.yaml build
# Run:     docker compose -f docker/compose.yaml up -d
# Dev:     docker compose -f docker/compose.yaml -f docker/compose.dev.yaml up
#
# Override the non-root UID/GID at build time if your host user isn't 1000:1000:
#   docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)

# ---- builder ------------------------------------------------------------

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1

# Bring in the uv binary from the official distroless image — faster + smaller
# than `pip install uv`.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install locked deps first (cached layer, only invalidated when uv.lock changes)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy source + install the project itself
COPY app ./app
COPY config ./config
COPY static ./static
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime ------------------------------------------------------------

FROM python:3.12-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# tini = clean PID 1 signal handling (cancels asyncio tasks on SIGTERM)
# curl = used by the HEALTHCHECK below
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. Override UID/GID at build time to match your host user.
ARG UID=1000
ARG GID=1000
RUN groupadd --system --gid ${GID} appuser \
 && useradd  --system --uid ${UID} --gid ${GID} --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy venv + source from the builder stage
COPY --from=builder --chown=appuser:appuser /app /app

# Writable runtime dirs. Volumes will normally mount over these, but the
# directories must exist for any fallback path (and for first-boot inside a
# disposable container).
RUN mkdir -p /app/data/snapshots /app/logs \
 && chown -R appuser:appuser /app/data /app/logs

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8001/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
