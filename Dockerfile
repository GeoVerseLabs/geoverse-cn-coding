# 单镜像部署：Python 运行时 + 内置行政区划数据 + 预建数据文件，无需 ES / PostGIS / Redis。
# 国内可替换为镜像加速地址：--build-arg BASE_IMAGE=<registry-mirror>/library/python:3.12-slim
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# 国内构建可传入镜像源：--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --index-url "${PIP_INDEX_URL}" . && useradd --system --uid 10001 app

COPY data ./data
# 镜像内用“全国区划（含中心点、历史别名）+ 示例 POI”预建一份数据，开箱即可试用；
# 生产环境把自建的 geocoder.db 挂载到 /data 并设置 GEOCODER_DB=/data/geocoder.db
RUN cngeocoder build --admin-dir data/admin \
        --places data/sample/places.csv \
        --roads data/sample/roads.geojson \
        --out /app/build/geocoder.db \
    && mkdir -p /data && chown app /data

ENV GEOCODER_DB=/app/build/geocoder.db \
    WORKERS=2
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status != 200)"
CMD ["sh", "-c", "exec cngeocoder serve --host 0.0.0.0 --port 8000 --workers ${WORKERS}"]
