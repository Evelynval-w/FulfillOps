
# Stage 1: Obtain the pinned uv executable
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

# Stage 2: Build the application environment
FROM python:3.13-slim AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install locked production dependencies first
COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

# Add application code and database migrations
COPY src ./src
COPY db/migrations ./db/migrations

# Install the FulfillOps application
RUN uv sync --frozen --no-dev --no-editable

# Stage 3: Minimal production runtime
FROM python:3.13-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    MIGRATIONS_DIR=db/migrations

WORKDIR /app

# Create an unprivileged runtime user
RUN groupadd --system app && \
    useradd --system --gid app --create-home app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/db/migrations /app/db/migrations

USER app

EXPOSE 8080

# Default command: API. Compose can override this for the worker.
CMD ["python", "-m", "fulfillops.api"]
