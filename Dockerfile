# 综述协作平台：一个镜像，三种部署形态（REVIEWDESK_PROFILE = local / server / cloud）
# 构建：docker build -t reviewdesk:latest .

FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REVIEWDESK_DATA_DIR=/data \
    REVIEWDESK_STATIC_DIR=/app/web
# pandoc 用于导出 Word 稿件（F6）；受限网络可 --build-arg INSTALL_PANDOC=0 跳过（仍可导出 Markdown + BibTeX）
ARG INSTALL_PANDOC=1
RUN if [ "$INSTALL_PANDOC" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends pandoc && rm -rf /var/lib/apt/lists/*; \
    fi
WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/reviewdesk backend/reviewdesk
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" "./backend[server]"
COPY --from=web /web/dist /app/web
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["reviewdesk", "serve", "--host", "0.0.0.0", "--port", "8000"]
