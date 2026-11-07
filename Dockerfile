# LeadScout production image
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    DB_PATH=/data/leadscout.db \
    PORT=8000

WORKDIR /app
COPY requirements.lock .
RUN pip install -r requirements.lock

COPY *.py sample_leads.csv sample_listings.csv ./
COPY static ./static

# Run as an unprivileged user; /data holds the SQLite file when DATABASE_URL isn't set
RUN useradd --create-home --uid 10001 leadscout && mkdir -p /data && chown leadscout /data
USER leadscout
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/readyz', timeout=4)"

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
