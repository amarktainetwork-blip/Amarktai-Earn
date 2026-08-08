FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client gnupg \
    && rm -rf /var/lib/apt/lists/*
RUN addgroup --system amarktai && adduser --system --ingroup amarktai amarktai
WORKDIR /app
COPY requirements.txt requirements-prod.txt /app/
RUN pip install --no-cache-dir -r requirements-prod.txt
COPY . /app
RUN chmod +x /app/scripts/*.sh \
    && mkdir -p /var/lib/amarktai-earn/{artifacts,jobs,repos,cache,backups,logs,uploads} \
    && chown -R amarktai:amarktai /app /var/lib/amarktai-earn
USER amarktai
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
