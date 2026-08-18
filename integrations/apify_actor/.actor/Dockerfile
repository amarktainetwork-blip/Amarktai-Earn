FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /usr/src/app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium
COPY src ./src
RUN python -m compileall -q src

RUN useradd --create-home --uid 10001 actor \
    && mkdir -p /ms-playwright \
    && chown -R actor:actor /usr/src/app /ms-playwright
USER 10001:10001

# Build-time smoke executes as the same non-root user as the final Actor and
# launches Chromium only against about:blank, proving the browser binary and
# runtime permissions without making any external request.
RUN python -m src.browser_smoke

CMD ["python", "-m", "src.main"]
