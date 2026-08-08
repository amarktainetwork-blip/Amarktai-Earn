FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io ca-certificates \
    && if ! command -v docker >/dev/null; then ln -s "$(command -v docker.io)" /usr/local/bin/docker; fi \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10002 --create-home --shell /usr/sbin/nologin broker
WORKDIR /app
COPY sandbox_broker /app/sandbox_broker
COPY scripts/dependency-prep-smoke.py /app/scripts/dependency-prep-smoke.py
USER 10002:10002
CMD ["python", "-m", "sandbox_broker.server"]
