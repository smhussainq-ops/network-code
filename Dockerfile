FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NETCODE_WORKSPACE=/data \
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
COPY inventories ./inventories
COPY static ./static

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[postgres]"

EXPOSE 8095

CMD ["sh", "-c", "python -c 'from netcode.bootstrap import init_workspace; from netcode.paths import paths; init_workspace(paths())' && uvicorn netcode.api:app --host 0.0.0.0 --port 8095"]
