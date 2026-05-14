# Resume Coach Cloud (v2)

简历教练云端版 — 完整 SaaS（账户体系 + 钱包 + 邀请码 + 管理后台）。

## 架构

- **后端**：Python 标准库 `http.server`（ThreadingHTTPServer），无第三方框架依赖
- **数据库**：SQLite WAL 模式，文件 `$DATA_DIR/app.db`
- **会话**：Cookie + PBKDF2 200K 迭代密码哈希
- **前端**：
  - `index.html`：Landing page（"你的 AI 求职副驾"）
  - `resume-coach.html`：主应用（10000+ 行单页 SPA）

## 部分端点速览

| 类型 | 端点 |
|---|---|
| 用户 | `/register` `/login` `/logout` `/me` `/me/wallet` `/me/resumes` `/me/companies` |
| 模型调用 | `/generate` `/chat` `/proxy-chat`（需登录 + 扣积分） |
| 计费 | `/billing-rules` `/redeem`（兑换码） |
| 任务 | `/tasks` |
| 管理后台 | `/admin/dashboard` `/admin/users` `/admin/invites` `/admin/recharges` |

详见 `server.py` 主 dispatch（`do_GET` / `do_POST`）。

## 本地跑

```bash
python3 server.py
# 访问 http://localhost:8765/
```

第一次跑需要创建管理员账号：

```bash
python3 server.py admin create
```

## 部署到 Zeabur

1. push 到 GitHub
2. zeabur.com → 项目 → 添加服务 → 选 `jiangjingyi496/resume-coach`
3. Zeabur 自动识别 Dockerfile 并构建
4. ⚠️ **必须挂 Volume 到 `/app/data`**，否则容器重启用户/积分数据全丢
5. 绑定域名（自动 HTTPS）
6. 容器启动后用 Zeabur 的「命令」Tab 进容器跑 `python3 server.py admin create` 建管理员

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `PORT` | `8765` | 服务监听端口；Zeabur 会注入 `8080` |
| `DATA_DIR` | `~/.resume-coach` | SQLite 数据库目录；云端指向挂载卷 `/app/data` |

## 关键路径

- SQLite 数据库：`$DATA_DIR/app.db` + `.db-wal` + `.db-shm`
- 上传图片：当前存 `/tmp/resume-coach-uploads/`（容器重启会丢，不重要）

## 与本地原版的差别

为部署到云端，相比 `~/Desktop/resume-coach-server.py` 原始版做了 4 处最小修改：

1. `PORT` 从硬编码 8765 → 读 `$PORT` 环境变量（默认 8765）
2. `DB_DIR` 从硬编码 `~/.resume-coach` → 读 `$DATA_DIR` 环境变量
3. server 监听地址：本地 `localhost`，云端（$PORT 注入时）`0.0.0.0`
4. `open_browser_after_delay()` 在云端 headless 环境自动 skip

**业务逻辑零改动**。
