# Chromium and fonts come with the Playwright image; keep the pip package on the same version.
ARG PLAYWRIGHT_VERSION=1.64.1
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble
ARG PLAYWRIGHT_VERSION

WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PROMPTS_DIR=/app/prompts

COPY pyproject.toml README.md ./
COPY inkbridge ./inkbridge
COPY prompts ./prompts
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" . && \
    apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*

# k8s/deployment.yaml runs as uid/gid 1001 with runAsNonRoot, which needs a numeric USER.
RUN test "$(id -u pwuser):$(id -g pwuser)" = "1001:1001"
USER 1001:1001
EXPOSE 8000
CMD ["inkbridge", "serve", "--port", "8000"]
