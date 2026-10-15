# 依赖在镜像构建阶段自动获取（pip install），无需宿主机安装 Python 或任何库
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    UI_PORT=8080 \
    TZ=Asia/Shanghai

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('UI_PORT','8080')+'/healthz',timeout=3)"

ENTRYPOINT ["python", "-u", "app/main.py"]
