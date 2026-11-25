FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

WORKDIR /app

# 配置阿里云 PyPI 镜像主源 + 官方 PyPI 备源（兼容魔搭香港/国内构建节点）并安装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --extra-index-url https://pypi.org/simple \
    --trusted-host mirrors.aliyun.com \
    -r requirements.txt

# 复制应用代码、静态资源（含 web/vendor）与种子/原始数据
COPY app ./app
COPY web ./web
COPY data ./data
COPY scripts ./scripts

# 清理可能残留的本地运行期数据库文件，确保容器首次启动从种子/原始数据幂等初始化
RUN rm -f data/*.db data/*.db-wal data/*.db-shm

EXPOSE 7860

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
