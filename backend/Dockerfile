# syntax=docker/dockerfile:1

# =============================================================================
# Base stage
# =============================================================================
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Create non-root user
RUN groupadd --gid 1000 appgroup && \
    useradd --uid 1000 --gid appgroup --shell /bin/bash --create-home appuser

# =============================================================================
# Development stage
# =============================================================================
FROM base AS development

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"]

# =============================================================================
# Production build stage
# =============================================================================
FROM base AS builder

COPY requirements.txt ./
RUN pip install --target=/app/dependencies -r requirements.txt

# =============================================================================
# Production stage
# =============================================================================
FROM base AS production

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Copy dependencies from builder
COPY --from=builder /app/dependencies /app/dependencies
ENV PYTHONPATH=/app/dependencies

# Copy application code
COPY --chown=appuser:appgroup app/ /app/app/

USER appuser

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
