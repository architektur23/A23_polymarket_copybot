# ── Stage 1: build dependencies ──────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools for wheels that need C extensions (web3, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim

# Non-root user (required for Unraid and good practice)
RUN groupadd -r botuser && useradd -r -g botuser -u 1000 botuser

WORKDIR /app

# Copy pre-built wheels and install without network access
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/* \
 && rm -rf /wheels

# Copy application source
COPY app/ ./app/

# Create data directory with correct ownership
RUN mkdir -p /app/data/logs && chown -R botuser:botuser /app/data

USER botuser

# Volume for persistent storage (SQLite DB + logs)
VOLUME ["/app/data"]

# FastAPI / Uvicorn
EXPOSE 2301

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:2301/health')"

# Graceful shutdown: uvicorn handles SIGTERM cleanly
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "2301", \
     "--workers", "1", \
     "--timeout-graceful-shutdown", "10"]
