FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal

ARG RELEASE_ID=development
LABEL org.opencontainers.image.version="${RELEASE_ID}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NETCODE_WORKSPACE=/data \
    NETCODE_STATIC_DIR=/app/static \
    NETCODE_EXECUTION=runner \
    NETCODE_RUNNER_POOL=default

WORKDIR /app

RUN microdnf update -y \
    && microdnf install -y \
       python3.12 python3.12-pip git-core openssh-clients ca-certificates \
    && microdnf clean all \
    && rm -rf /var/cache/dnf

COPY pyproject.toml README.md ./
COPY netcode ./netcode
COPY templates ./templates
COPY policies ./policies
COPY static ./static

RUN python3.12 -m pip install --no-cache-dir --upgrade pip \
    && python3.12 -m pip install --no-cache-dir ".[postgres]" \
    && rm -rf /app/build /root/.cache

RUN mkdir -p /data && chown -R 65534:65534 /data

EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3.12 -c "import os,urllib.request; h=(os.getenv('NETCODE_ALLOWED_HOSTS','localhost').split(',')[0].strip() or 'localhost'); q=urllib.request.Request('http://127.0.0.1:8095/api/ready',headers={'Host':h}); urllib.request.urlopen(q,timeout=5).read()" || exit 1

USER 65534:65534

CMD ["python3.12", "-m", "uvicorn", "netcode.api:app", "--host", "0.0.0.0", "--port", "8095", "--workers", "1"]
