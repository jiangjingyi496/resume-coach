# Resume Coach Cloud

简历教练云端版（MVP 阶段 1）。

## 架构

- **后端**：FastAPI（Python 3.11+）
- **数据库**：SQLite（运行时存 `/app/data/resume-coach.db`）
- **前端**：单 HTML（`static/resume-coach.html`），同源调 API
- **模型**：用户在前端「模型设置」里自填 OpenAI 兼容 API（key 不入服务端日志）

## 端点

| Method | Path | 说明 |
|---|---|---|
| GET | `/` | 服务前端 HTML |
| GET | `/health` | 健康检查 |
| POST | `/proxy-chat` | 转发到用户配置的 OpenAI 兼容 API |
| POST | `/upload-image` | 接收 base64 dataUrl，存 SQLite blob |
| GET | `/image/{id}` | 读图片 |
| POST | `/generate` | ❌ 503（本地 CLI 模式不可用） |
| POST | `/chat` | ❌ 503（同上） |
| GET | `/admin/stats` | 最近请求统计（暂无 auth） |

## 本地跑

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8765 --reload
# 访问 http://localhost:8765/
```

或者 Docker：

```bash
docker build -t resume-coach .
docker run -p 8765:8765 -v $(pwd)/data:/app/data resume-coach
```

## 部署到 Zeabur

1. push 到 GitHub
2. zeabur.com → 新建 Project → Service → Deploy from GitHub
3. 选这个 repo，Zeabur 自动识别 Dockerfile 并构建
4. 完成后绑定一个 `.zeabur.app` 子域名（自动 HTTPS）

## 路线图

- [x] 阶段 1：FastAPI + SQLite + Zeabur 部署
- [ ] 阶段 2：GitHub OAuth 登录，数据库迁 Postgres，加配额
- [ ] 阶段 3：接支付（Stripe / 易支付），按调用次数扣费
