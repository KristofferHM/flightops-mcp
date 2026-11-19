# syntax=docker/dockerfile:1
# Multi-stage build: resolve dependencies once, then ship a slim non-root image.

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

# Non-root by default: an MCP server holds upstream credentials, so a container
# escape is not the only thing to defend against — a writable /usr is too.
#
# First, Debian security fixes that landed after the base image was published. The image scan
# in CI gates on fixable HIGH and CRITICAL findings; this is what keeps "fixable" from meaning
# "fixed in Debian, not in our image" (docs/SECURITY.md, ADR 0006).
RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 flightops \
    && useradd --system --uid 1001 --gid flightops --create-home flightops

WORKDIR /app
COPY --from=builder --chown=flightops:flightops /app/.venv /app/.venv
COPY --from=builder --chown=flightops:flightops /app/src /app/src
COPY --chown=flightops:flightops data/airports_world.csv /app/data/airports_world.csv
COPY --chown=flightops:flightops scripts/build_airports_db.py /app/scripts/build_airports_db.py

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FLIGHTOPS_AIRPORTS_DB=/app/data/airports.sqlite3 \
    FLIGHTOPS_LOG_LEVEL=INFO

# Bake the airport reference database at build time so the image starts with no
# network dependency: the committed world set, 3,270 scheduled large and medium airports.
RUN python scripts/build_airports_db.py --csv data/airports_world.csv --db "$FLIGHTOPS_AIRPORTS_DB" \
    && chown -R flightops:flightops /app/data

USER flightops
EXPOSE 8000

# Smoke test without a shell: the server must be importable and its tools registered.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import flightops_mcp.server as s; s.mcp.name"]

ENTRYPOINT ["flightops-mcp"]
CMD ["--transport", "stdio"]
