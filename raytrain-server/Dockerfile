# raytrain-server (control plane)
#
# Multi-stage build:
#   - Stage 1 installs deps into a venv at /opt/venv
#   - Stage 2 copies the venv + source into a slim runtime image
#
# Build:
#   docker build -t 172.31.9.104:5050/raytrain/raytrain-server:v0.1 \
#       -f raytrain-server/Dockerfile raytrain-server/
#
# Run locally for smoke:
#   docker run --rm -p 8080:8080 \
#     -e RAYTRAIN_JWT_SECRET=$(openssl rand -hex 32) \
#     172.31.9.104:5050/raytrain/raytrain-server:v0.1
#
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
COPY pyproject.toml .
COPY raytrain_server ./raytrain_server
RUN pip install --upgrade pip && \
    pip install ".[postgres]" && \
    # Strip build-time deps to keep the venv lean
    pip uninstall -y pip setuptools wheel || true

# ---------------------------------------------------------------------------- #

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    RAYTRAIN_SERVER_HOST=0.0.0.0 \
    RAYTRAIN_SERVER_PORT=8080

# Create non-root user for the server. Ray Job Submission API is HTTP, so we
# do NOT need to be root.
RUN groupadd -r raytrain && useradd -r -g raytrain -u 10001 raytrain

COPY --from=builder /opt/venv /opt/venv

USER raytrain
WORKDIR /home/raytrain

EXPOSE 8080

# Health check goes through the FastAPI /healthz route. Kubernetes will use
# its own probes (see deploy/deployment.yaml); this is for `docker run` users.
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request,sys; \
                    urllib.request.urlopen('http://127.0.0.1:8080/healthz').read(); \
                    sys.exit(0)"

CMD ["uvicorn", "raytrain_server.main:app", \
     "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
