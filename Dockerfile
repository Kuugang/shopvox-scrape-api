FROM alpeware/chrome-headless-trunk

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential wget libssl-dev zlib1g-dev \
      libncurses5-dev libffi-dev libsqlite3-dev \
      libreadline-dev libbz2-dev && \
    wget https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz && \
    tar -xzf Python-3.11.9.tgz && \
    cd Python-3.11.9 && \
    ./configure --enable-optimizations && \
    make -j$(nproc) && make install && \
    cd .. && rm -rf Python-3.11.9*

RUN ln -sf /usr/bin/python3.11 /usr/bin/python3 && python3 -m pip install --upgrade pip
WORKDIR /app
COPY shopvox_scrape_api.py /app/shopvox_scrape_api.py

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt

RUN pip install --upgrade pip \
 && pip install --no-cache-dir --prefer-binary -r /tmp/requirements.txt

# Default env
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    CDP_PORT=9222 \
    PW_CDP_URL=http://127.0.0.1:9222/json/version

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null || exit 1

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

