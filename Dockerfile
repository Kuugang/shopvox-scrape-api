FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PW_CDP_URL=http://host.docker.internal:9222

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY shopvox_scrape_api.py /app/shopvox_scrape_api.py

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" python-dotenv playwright

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null || exit 1

CMD ["sh", "-lc", "python -m uvicorn shopvox_scrape_api:app --host 0.0.0.0 --port ${PORT}"]
