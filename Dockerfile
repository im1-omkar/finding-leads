FROM python:3.11-slim

# Prevent Python from writing .pyc files, keep output unbuffered, and configure paths
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOST=0.0.0.0 \
    PORT=8000

# Install runtime dependencies (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and app directory
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

WORKDIR /app

# Copy dependency definition and install third-party packages first (cached layer)
COPY --chown=appuser:appuser lead-research-crm-loader/pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[dev]"

# Copy application source code and scenarios
COPY --chown=appuser:appuser lead-research-crm-loader/lead_crm_loader/ lead_crm_loader/
COPY --chown=appuser:appuser lead-research-crm-loader/tests/ tests/
COPY --chown=appuser:appuser lead-research-crm-loader/examples/ examples/
COPY --chown=appuser:appuser lead-research-crm-loader/ARCHITECTURE.md lead-research-crm-loader/README.md ./
COPY --chown=appuser:appuser lead-research-crm-loader/.env.example ./

# Install package itself now that source files are present
RUN pip install --no-cache-dir --no-deps -e .

# Copy default .env if not mounted/provided
RUN cp .env.example .env && chown appuser:appuser .env

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "lead_crm_loader"]
