# Production image for Railway (or any container host).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENVIRONMENT=production \
    LOG_JSON=true \
    FORAGER_CACHE_DIR=/data/forager

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# Run as an unprivileged user; /data holds the Forager cache (mount a volume to persist it).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/forager \
    && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000
# Railway injects $PORT. --proxy-headers so X-Forwarded-Proto is honoured behind its proxy.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
