FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl bash build-essential nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/aider-venv \
    && /opt/aider-venv/bin/pip install --no-cache-dir aider-chat==0.86.2 \
    && ln -s /opt/aider-venv/bin/aider /usr/local/bin/aider
RUN python -m venv /opt/openhands-venv \
    && /opt/openhands-venv/bin/pip install --no-cache-dir openhands==1.16.0 \
    && ln -s /opt/openhands-venv/bin/openhands /usr/local/bin/openhands
RUN pip install --no-cache-dir pytest==8.4.2 ruff==0.12.11
RUN useradd --uid 10001 --create-home --shell /bin/bash sandbox \
    && mkdir -p /workspace /opt/amarktai \
    && chown -R sandbox:sandbox /workspace /home/sandbox
COPY sandbox/run-agent.sh /opt/amarktai/run-agent
RUN chmod 0755 /opt/amarktai/run-agent
WORKDIR /workspace
USER 10001:10001
CMD ["/opt/amarktai/run-agent"]
