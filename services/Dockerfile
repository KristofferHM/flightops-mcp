# syntax=docker/dockerfile:1
# One image for the local test services. Compose picks the app with `command`.
# These are fixtures for the auth matrix; nothing here belongs in production.

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --group services


FROM python:3.12-slim AS runtime

RUN groupadd --system --gid 1001 services \
    && useradd --system --uid 1001 --gid services --create-home services

WORKDIR /app
COPY --from=builder --chown=services:services /app/.venv /app/.venv
COPY --chown=services:services services /app/services

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER services

ENTRYPOINT ["uvicorn", "--factory", "--host", "0.0.0.0"]
CMD ["services.idp.app:create_app", "--port", "9000"]
