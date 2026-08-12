# =============================================================================
# Backend image.
#
# Two stages so build tooling never reaches the runtime image, and the runtime
# runs as an unprivileged user with no package manager available to it.
# =============================================================================

FROM python:3.12-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build
# Dependency metadata first: this layer is cached across source changes.
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ backend/
RUN uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Unprivileged, no login shell, no home directory to write into.
RUN groupadd --system --gid 1001 clinic \
 && useradd --system --uid 1001 --gid clinic --no-create-home --shell /usr/sbin/nologin clinic

WORKDIR /app
COPY --from=builder --chown=root:root /build/.venv /app/.venv
COPY --from=builder --chown=root:root /build/backend /app/backend

WORKDIR /app/backend
USER clinic

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health/live')"

# Migrations are deliberately NOT run here: schema changes are an explicit,
# audited operational step, not a side effect of a container restart.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
