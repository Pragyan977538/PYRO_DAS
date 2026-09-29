# FireWatch API and map: one process serves both (see firewatch/api/main.py).
# Everything the page loads is local -- vendored MapLibre, the Natural Earth
# basemap from DATA_DIR/basemap -- so it runs with the network disconnected.
FROM python:3.13-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY firewatch firewatch
COPY scripts scripts
COPY sql sql
COPY web web
COPY reference reference

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"
# Apply migrations (idempotent), then serve.
CMD ["sh", "-c", "python scripts/migrate.py && python -m uvicorn firewatch.api.main:app --host 0.0.0.0 --port 8000"]
