FROM python:3.11-slim

# 系统依赖最少化（v2 server.py 只用 Python 标准库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# v2 是纯标准库，requirements.txt 实际为空（保留文件以备将来扩展）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码：server.py + 两个 HTML 都和 server.py 同目录
COPY server.py .
COPY resume-coach.html .
COPY index.html .

# 数据目录（SQLite 文件挂在这里，Zeabur Volume 会挂到 /app/data）
RUN mkdir -p /app/data
ENV DATA_DIR=/app/data

# 端口：Zeabur / Railway 通过 $PORT 注入运行时端口（通常 8080）
# server.py 已经改成读 os.environ.get('PORT', 8765)
EXPOSE 8080

# 直接跑 server.py，无需 uvicorn（v2 用 ThreadingHTTPServer 自己处理并发）
CMD ["python3", "server.py"]
