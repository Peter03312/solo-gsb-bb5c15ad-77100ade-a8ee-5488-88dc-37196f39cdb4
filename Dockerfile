FROM python:3.12-slim

# 不生成 .pyc、日志直接输出，便于容器观察
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先单独安装依赖，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝应用代码
COPY app ./app

# 以非 root 用户运行
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); assert json.load(r)['status']=='ok'" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
