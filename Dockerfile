FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (separate layer — only re-runs when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app/ ./app/

# Data directory for devices.json and module_lookup.json (mount volumes here)
RUN mkdir -p /data

# Defaults — all overridable at runtime via environment variables or docker-compose
ENV MODULE_LOOKUP_PATH=/data/module_lookup.json \
    DATA_FILE=/data/devices.json \
    REFRESH_INTERVAL_HOURS=6 \
    SNMP_TIMEOUT=3 \
    SNMP_RETRIES=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
