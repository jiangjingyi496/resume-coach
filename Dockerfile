FROM python:3.11-slim

# 系统依赖最少化：只需要构建 httpx / pydantic 的 wheel
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker layer cache）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY main.py .
COPY static/ ./static/

# 创建数据目录（SQLite 文件会写在这里）
RUN mkdir -p /app/data
ENV DATA_DIR=/app/data

# 默认端口；Zeabur / Railway 会通过 $PORT 注入运行时端口
ENV PORT=8765
EXPOSE 8765

# uvicorn 启动，绑 0.0.0.0:$PORT
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8765}"]
