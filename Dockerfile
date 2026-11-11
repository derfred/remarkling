# Chromium and fonts come with the Playwright image; uv.lock must pin the playwright package to the same version.
ARG PLAYWRIGHT_VERSION=1.64.1
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble
ARG PLAYWRIGHT_VERSION
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PROMPTS_DIR=/app/prompts \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH=/app/.venv/bin:$PATH

# Install the Python version specified in .python-version using uv.
COPY .python-version ./
RUN UV_PYTHON_DOWNLOADS=automatic uv python install

RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*

# Dependencies first so they cache independently of source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project && \
    test "$(python -c 'import importlib.metadata as m; print(m.version("playwright"))')" = "${PLAYWRIGHT_VERSION}"

COPY README.md ./
COPY inkbridge ./inkbridge
COPY prompts ./prompts
RUN uv sync --locked --no-dev --no-editable

# k8s/deployment.yaml runs as uid/gid 1001 with runAsNonRoot, which needs a numeric USER.
RUN test "$(id -u pwuser):$(id -g pwuser)" = "1001:1001"
USER 1001:1001
EXPOSE 8000
CMD ["inkbridge", "serve", "--port", "8000"]
