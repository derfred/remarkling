# syntax=docker/dockerfile:1
# Chromium and fonts come with the Playwright image; uv.lock must pin the playwright package to the same version.
ARG PLAYWRIGHT_VERSION=1.64.1

# ---------- web: the cache explorer UI (Svelte + Tailwind), built into inkbridge/web ----------
FROM node:24-slim AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/ ./
RUN npm run build

# ---------- builder: interpreter + venv ----------
# Same base as the runtime: its layers are pulled anyway, and glibc/paths match exactly.
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble AS builder
ARG PLAYWRIGHT_VERSION
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

# Install the Python version specified in .python-version using uv.
COPY .python-version ./
RUN uv python install

# Dependencies first so they cache independently of source changes.
# The uv cache lives in a BuildKit cache mount, so it never ends up in a layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project && \
    test "$(.venv/bin/python -c 'import importlib.metadata as m; print(m.version("playwright"))')" = "${PLAYWRIGHT_VERSION}"

COPY README.md ./
COPY inkbridge ./inkbridge
COPY --from=web /app/inkbridge/web ./inkbridge/web
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------- runtime ----------
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble

RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PROMPTS_DIR=/app/prompts \
    PATH=/app/.venv/bin:$PATH

# The venv's python symlinks into /opt/uv-python and script shebangs point at /app/.venv,
# so both must keep the same paths as in the builder.
COPY --from=builder /opt/uv-python /opt/uv-python
COPY --from=builder /app/.venv /app/.venv
COPY prompts ./prompts

# k8s/deployment.yaml runs as uid/gid 1001 with runAsNonRoot, which needs a numeric USER.
RUN test "$(id -u pwuser):$(id -g pwuser)" = "1001:1001"
USER 1001:1001
EXPOSE 8000
CMD ["inkbridge", "serve", "--port", "8000"]
