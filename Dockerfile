# Production lean Dockerfile for Telegram News Monitor
FROM python:3.11-slim AS runtime

# Ensure standard output is delivered directly to console without buffering
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

# Create non-root system user and group (UID 10001)
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/sh -m appuser

WORKDIR /app

# Install runtime system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl sqlite3 && \
    rm -rf /var/lib/apt/lists/*

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

# Container healthcheck ensuring Python runtime integrity and CLI responsiveness
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -m tg_news_monitor.main --version || exit 1

# Headless service entrypoint
ENTRYPOINT ["python", "-m", "tg_news_monitor.main"]
