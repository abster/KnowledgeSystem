# Toolchain image for `verify` and `eval`. The submission itself needs no Docker.
FROM python:3.12.11-slim-bookworm

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends make \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.6

WORKDIR /work
COPY pyproject.toml uv.lock ./
# Dependencies only; the repository is mounted at /work by docker-compose.
RUN uv sync --frozen --no-install-project
