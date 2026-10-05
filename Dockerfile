# Notana Care planning simulator: FastAPI + OR-Tools backend serving the static frontend.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    NOTANA_DB=/data/notana.sqlite3 NOTANA_CACHE_DIR=/data/cache PORT=8000

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY frontend frontend
RUN mkdir -p /data && useradd -m app && chown -R app /data
USER app
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"
# One process on purpose: live sessions are held in memory.
CMD ["sh", "-c", "uvicorn notana_planner.api:app --host 0.0.0.0 --port ${PORT} --workers 1"]
