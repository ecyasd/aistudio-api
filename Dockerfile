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

FROM python:3.12-slim-bookworm
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src PYTHON_DOTENV_DISABLED=1 \
    AISTUDIO_DATA_DIR=/data AISTUDIO_CONFIG_FILE=/data/config.yaml WAA_STATE_DIR=/data/waa
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl libstdc++6 libgcc-s1 \
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
RUN /app/waa-signer/bin/waa-signer -h >/dev/null 2>&1
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 CMD curl -fsS http://127.0.0.1:8080/health || exit 1
CMD ["python", "/app/deploy/container_entrypoint.py"]
