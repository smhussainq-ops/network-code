FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NETCODE_WORKSPACE=/data \
    NETCODE_STATIC_DIR=/app/static \
    NETCODE_EXECUTION=runner \
    NETCODE_RUNNER_POOL=default

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY netcode ./netcode
COPY templates ./templates
COPY policies ./policies
COPY static ./static

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[postgres]" \
    && rm -rf /app/build /root/.cache

RUN mkdir -p /data && chown -R nobody:nogroup /data

EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; h=(os.getenv('NETCODE_ALLOWED_HOSTS','localhost').split(',')[0].strip() or 'localhost'); q=urllib.request.Request('http://127.0.0.1:8095/api/ready',headers={'Host':h}); urllib.request.urlopen(q,timeout=5).read()" || exit 1

USER nobody

CMD ["uvicorn", "netcode.api:app", "--host", "0.0.0.0", "--port", "8095", "--workers", "1"]
