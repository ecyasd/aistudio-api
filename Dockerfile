FROM node:24-bookworm-slim AS frontend
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM golang:1.27-bookworm AS signer
WORKDIR /build/waa-signer
COPY waa-signer/ ./
RUN bash build.sh

FROM debian:bookworm-slim AS browser
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fL --retry 3 https://github.com/CloakHQ/CloakBrowser/releases/download/chromium-v146.0.7680.177.4/cloakbrowser-linux-x64.tar.gz -o /tmp/browser.tar.gz \
    && echo '5af027faafb1fef9933eb784c094b764706de22a372a2cee84bc117fc4ab537f  /tmp/browser.tar.gz' | sha256sum -c - \
    && mkdir -p /opt/cloakbrowser && tar -xzf /tmp/browser.tar.gz -C /opt/cloakbrowser --strip-components=1 \
    && rm /tmp/browser.tar.gz

FROM python:3.12-slim-bookworm
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src PYTHON_DOTENV_DISABLED=1 \
    AISTUDIO_DATA_DIR=/data AISTUDIO_CONFIG_FILE=/data/config.yaml AISTUDIO_ACCOUNTS_DIR=/data/accounts \
    AISTUDIO_STATS_FILE=/data/stats.json AISTUDIO_ROTATOR_STATE_FILE=/data/rotator_state.json \
    AISTUDIO_LOG_DIR=/logs WAA_STATE_DIR=/data/waa HOME=/data XDG_CACHE_HOME=/data/cache AISTUDIO_ALLOW_RESTART=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl libstdc++6 libgcc-s1 \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 aistudio && useradd --uid 10001 --gid 10001 --no-create-home aistudio
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY defaults/config.yaml ./defaults/config.yaml
COPY deploy/container_entrypoint.py ./deploy/container_entrypoint.py
COPY deploy/smoke_image.py ./deploy/smoke_image.py
COPY --from=frontend /build/src/aistudio_api/static/ ./src/aistudio_api/static/
COPY --from=signer /build/waa-signer/bin/waa-signer ./waa-signer/bin/waa-signer
COPY --from=browser /opt/cloakbrowser/ /opt/cloakbrowser/
RUN /app/waa-signer/bin/waa-signer -h >/dev/null 2>&1
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 CMD curl -fsS http://127.0.0.1:8080/health || exit 1
CMD ["python", "/app/deploy/container_entrypoint.py"]
