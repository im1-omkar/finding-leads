FROM python:3.11-slim

# Prevent Python from writing .pyc files, keep output unbuffered, and configure paths
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOST=0.0.0.0 \
    PORT=8000

# Install runtime dependencies (curl for healthcheck, libpq for psycopg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and app directory
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app && \
    chown -R appuser:appuser /app

WORKDIR /app

# Copy package metadata and source before the editable install so setuptools can discover it.
COPY --chown=appuser:appuser lead-research-crm-loader/pyproject.toml ./
COPY --chown=appuser:appuser lead-research-crm-loader/lead_crm_loader/ lead_crm_loader/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[dev]"

# Copy scenarios and documentation
COPY --chown=appuser:appuser lead-research-crm-loader/tests/ tests/
COPY --chown=appuser:appuser lead-research-crm-loader/examples/ examples/
COPY --chown=appuser:appuser lead-research-crm-loader/ARCHITECTURE.md lead-research-crm-loader/README.md ./
COPY --chown=appuser:appuser lead-research-crm-loader/.env.example ./

# Copy default .env if not mounted/provided
RUN cp .env.example .env && chown appuser:appuser .env

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "lead_crm_loader"]
