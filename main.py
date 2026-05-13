"""
Resume Coach 云端版 (Cloud Edition)

阶段 1 MVP：
- 端口由 PORT 环境变量决定（Zeabur / Railway / Fly.io 都遵守这个约定）
- /proxy-chat 是核心：转发到用户在前端填的 OpenAI-compatible API
- /generate 和 /chat 走本地 claude CLI 的逻辑在云端不可用，返回 503
- 所有 /proxy-chat 调用入库 SQLite（日志、复盘、未来计费基础）
- 图片上传暂存 SQLite blob（容器重启会丢，MVP 接受）

未来阶段：
- 阶段 2：加用户登录（GitHub OAuth），数据库改 Postgres，加配额
- 阶段 3：接支付（Stripe / 易支付），按调用次数扣费
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------- 配置 ----------
APP_ROOT = Path(__file__).parent
STATIC_DIR = APP_ROOT / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "resume-coach.db"
UPSTREAM_TIMEOUT_SECONDS = int(os.environ.get("UPSTREAM_TIMEOUT", "600"))

app = FastAPI(title="Resume Coach Cloud", version="0.1.0")

# 前端目前打算和后端同域部署，CORS 允许所有源用于调试便利（生产可收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 数据库 ----------
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_log (
                id          TEXT PRIMARY KEY,
                ts          INTEGER NOT NULL,
                client_ip   TEXT,
                endpoint    TEXT NOT NULL,
                model       TEXT,
                base_url    TEXT,
                msg_count   INTEGER,
                req_chars   INTEGER,
                resp_chars  INTEGER,
                duration_ms INTEGER,
                http_status INTEGER,
                error       TEXT
            );

            CREATE TABLE IF NOT EXISTS image_upload (
                id        TEXT PRIMARY KEY,
                ts        INTEGER NOT NULL,
                client_ip TEXT,
                mime      TEXT,
                size      INTEGER,
                data      BLOB
            );

            CREATE INDEX IF NOT EXISTS idx_chat_log_ts ON chat_log(ts);
            """
        )


@contextmanager
def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


init_db()


# ---------- 工具 ----------
def client_ip(request: Request) -> str:
    # Zeabur / Railway 在 X-Forwarded-For 里给真实 IP
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ---------- 模型 ----------
class Message(BaseModel):
    role: str
    content: str


class ProxyChatRequest(BaseModel):
    baseUrl: str = Field(..., description="OpenAI 兼容 API 的 base URL")
    apiKey: str
    modelName: str
    system: str = ""
    user: str = ""
    messages: Optional[List[Message]] = None
    temperature: float = 0.7


class UploadImageRequest(BaseModel):
    dataUrl: str
    filename: str = "image"


# ---------- 端点 ----------
@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": "0.1.0"}


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "resume-coach.html", media_type="text/html")


# 兼容老前端：/index.html 也指向同一个文件
@app.get("/index.html")
def index_html() -> FileResponse:
    return FileResponse(STATIC_DIR / "resume-coach.html", media_type="text/html")


@app.post("/generate")
def generate_disabled() -> JSONResponse:
    """
    本地版用 subprocess 调 claude CLI。云端容器里没有 CLI，明确返回 503。
    用户应该改用 /proxy-chat（前端「模型设置」里配置一个 OpenAI 兼容 API）。
    """
    return JSONResponse(
        status_code=503,
        content={
            "error": "CLI_NOT_AVAILABLE",
            "message": (
                "云端版不支持本地 Claude CLI。请在右上角「模型设置」里配置一个"
                "OpenAI 兼容 API（DeepSeek / 智谱 / Kimi / GPT 等都支持），"
                "前端会自动切换到 /proxy-chat 路径。"
            ),
        },
    )


@app.post("/chat")
def chat_disabled() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "CLI_NOT_AVAILABLE",
            "message": (
                "云端版不支持本地 Claude CLI。请在右上角「模型设置」里配置一个"
                "OpenAI 兼容 API。"
            ),
        },
    )


@app.post("/proxy-chat")
async def proxy_chat(req: ProxyChatRequest, request: Request) -> dict[str, Any]:
    """
    转发到 OpenAI 兼容 API（用户在前端配置的）。
    支持单轮（user 字段）或多轮（messages 数组）。
    """
    base_url = req.baseUrl.strip().rstrip("/")
    if not base_url.endswith("/chat/completions"):
        url = base_url + "/chat/completions"
    else:
        url = base_url

    # 拼 messages
    formatted: list[dict[str, str]] = []
    if req.system:
        formatted.append({"role": "system", "content": req.system})
    if req.messages:
        for m in req.messages:
            if m.role in ("user", "assistant") and m.content:
                formatted.append({"role": m.role, "content": m.content})
    elif req.user:
        formatted.append({"role": "user", "content": req.user})
    else:
        raise HTTPException(400, "需要 user 字段或 messages 数组")

    payload: dict[str, Any] = {
        "model": req.modelName,
        "messages": formatted,
        "temperature": req.temperature,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {req.apiKey}",
        "Content-Type": "application/json",
    }

    log_id = uuid.uuid4().hex
    ts = int(time.time())
    ip = client_ip(request)
    req_chars = sum(len(m["content"]) for m in formatted)
    start = time.time()
    error_msg: Optional[str] = None
    http_status: int = 0
    resp_text = ""

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
            r = await client.post(url, json=payload, headers=headers)
        http_status = r.status_code
        if r.status_code >= 400:
            error_msg = f"{r.status_code}: {r.text[:500]}"
            raise HTTPException(r.status_code, f"上游 API 返回 {r.status_code}：{r.text[:500]}")
        data = r.json()
        resp_text = data["choices"][0]["message"]["content"]
    except HTTPException:
        raise
    except httpx.TimeoutException:
        error_msg = "上游超时"
        raise HTTPException(504, f"上游 API 超时（>{UPSTREAM_TIMEOUT_SECONDS}s）")
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as e:
        error_msg = f"响应格式错误: {e}"
        raise HTTPException(502, f"上游响应格式不符合 OpenAI 规范：{e}")
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        raise HTTPException(502, f"上游请求失败：{e}")
    finally:
        # 不论成败都记录一笔日志
        duration_ms = int((time.time() - start) * 1000)
        try:
            with db_conn() as con:
                con.execute(
                    "INSERT INTO chat_log "
                    "(id, ts, client_ip, endpoint, model, base_url, msg_count, "
                    " req_chars, resp_chars, duration_ms, http_status, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        log_id, ts, ip, "/proxy-chat", req.modelName, base_url,
                        len(formatted), req_chars, len(resp_text), duration_ms,
                        http_status, error_msg,
                    ),
                )
        except Exception:
            # 日志失败不能影响主流程
            pass

    return {"text": resp_text}


@app.post("/upload-image")
def upload_image(body: UploadImageRequest, request: Request) -> dict[str, Any]:
    if ";base64," not in body.dataUrl:
        raise HTTPException(400, "需要 base64 data URL")
    try:
        mime_part, b64 = body.dataUrl.split(";base64,", 1)
        mime = mime_part.replace("data:", "")
        img_bytes = base64.b64decode(b64)
    except Exception as e:
        raise HTTPException(400, f"解码失败：{e}")

    img_id = uuid.uuid4().hex
    with db_conn() as con:
        con.execute(
            "INSERT INTO image_upload (id, ts, client_ip, mime, size, data) "
            "VALUES (?,?,?,?,?,?)",
            (img_id, int(time.time()), client_ip(request), mime, len(img_bytes), img_bytes),
        )
    return {"id": img_id, "size": len(img_bytes), "path": f"/image/{img_id}"}


@app.get("/image/{img_id}")
def get_image(img_id: str) -> Response:
    with db_conn() as con:
        row = con.execute(
            "SELECT mime, data FROM image_upload WHERE id = ?", (img_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "图片不存在")
    return Response(content=row["data"], media_type=row["mime"] or "image/png")


# 一个轻量的统计端点（你自己看，将来可以加 admin auth）
@app.get("/admin/stats")
def admin_stats() -> dict[str, Any]:
    with db_conn() as con:
        total = con.execute("SELECT COUNT(*) AS c FROM chat_log").fetchone()["c"]
        recent = con.execute(
            "SELECT id, ts, client_ip, model, msg_count, req_chars, resp_chars, "
            "duration_ms, http_status, error FROM chat_log "
            "ORDER BY ts DESC LIMIT 20"
        ).fetchall()
        images = con.execute("SELECT COUNT(*) AS c FROM image_upload").fetchone()["c"]
    return {
        "total_chat_requests": total,
        "total_images": images,
        "recent": [dict(r) for r in recent],
    }


# 静态资源挂载（前端如果以后引入 JS/CSS 文件可以放 static/ 下）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
