# Production lean Dockerfile for Telegram News Monitor
FROM python:3.11-slim AS runtime

# Ensure standard output is delivered directly to console without buffering.
# /usr/local/bin is where the official Node tarball and `npm i -g` put binaries,
# so the non-root appuser can run `codebuddy` without a custom npm prefix.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PATH="/usr/local/bin:${PATH}"

# Create non-root system user and group (UID 10001)
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/sh -m appuser

WORKDIR /app

# Runtime OS deps + Node 20 (x64/arm64) + pinned CodeBuddy CLI.
# Install as root so the global bin lands in /usr/local/bin (on PATH for appuser).
# Do not set CODEBUDDY_INTERNET_ENVIRONMENT in the image (international site).
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl sqlite3 xz-utils && \
    arch="$(uname -m)" && \
    case "$arch" in \
      x86_64) node_arch=x64 ;; \
      aarch64) node_arch=arm64 ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac && \
    curl -fsSL "https://nodejs.org/dist/v20.19.5/node-v20.19.5-linux-${node_arch}.tar.xz" \
      | tar -xJ -C /usr/local --strip-components=1 && \
    npm install -g @tencent-ai/codebuddy-code@2.149.0 && \
    npm cache clean --force && \
    test -x /usr/local/bin/codebuddy && \
    apt-get purge -y --auto-remove xz-utils && \
    rm -rf /var/lib/apt/lists/* /root/.npm /tmp/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source tree
COPY src/ /app/src/
COPY pyproject.toml .

# Prepare data storage directory and assign ownership to non-root appuser
RUN mkdir -p /app/data && \
    chown -R appuser:appgroup /app

# Switch to non-root user for principle of least privilege
USER appuser:appgroup

# Business heartbeat: recent ingest + eval. Compose `restart` does not
# recreate the container on unhealthy alone (needs an autoheal sidecar).
HEALTHCHECK --interval=60s --timeout=15s --start-period=180s --retries=3 \
    CMD python -m tg_news_monitor.main --healthcheck || exit 1

# Headless service entrypoint
ENTRYPOINT ["python", "-m", "tg_news_monitor.main"]
