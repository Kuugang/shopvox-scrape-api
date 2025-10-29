FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=0

WORKDIR /app

COPY requirements.txt ./

RUN pip install -r requirements.txt

RUN python -m playwright install --with-deps chromium

COPY . ./

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "hypercorn main:app --bind 0.0.0.0:${PORT:-8000}"]
