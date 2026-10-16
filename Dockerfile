FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Embedding model baked into the image (below) so it isn't re-downloaded on every deploy
    FASTEMBED_CACHE_PATH=/opt/fastembed

WORKDIR /app

# Install deps first so this layer is cached across code-only changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# The Copilot's local embedding model (~130 MB). Must match COPILOT_EMBED_MODEL.
ARG EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBED_MODEL}')"

COPY . .

# Run as a non-root user. /app/output is the persistent data volume in production
# (/opt/salesai/data/output on the host, owned by uid 1000 — see deploy/setup_server.sh).
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/output \
    && chown -R app:app /app/output /opt/fastembed
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=8).status == 200 else 1)"

# ONE worker on purpose: the embedded Chroma vector store and the in-memory research-job
# registry both belong to a single process (DEPLOYMENT_REVIEW.md B3). Background loops run
# as threads in this process and use Postgres advisory locks.
# --proxy-headers: trust nginx's X-Forwarded-* so the app sees the real client IP and https.
# Safe with "*" because the container port is only published on the host's 127.0.0.1.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
