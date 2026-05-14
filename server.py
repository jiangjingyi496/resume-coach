#!/usr/bin/env python3
"""
Resume Coach 本地后端 — 稳定版

V2 改进：
  - ThreadingHTTPServer 并发处理（health 不再被长请求阻塞）
  - 所有 socket 写入吞掉 BrokenPipeError（浏览器超时断开不再崩 handler）
  - 启动时主动检测端口占用，给出明确提示
  - daemon_threads 让子线程不阻塞进程退出
  - 自定义 finish() 避免 wfile.flush 异常

启动方式：
  python3 ~/Desktop/resume-coach-server.py
  推荐用 watcher 包装实现自动重启：~/Desktop/resume-coach-watcher.sh
"""
import getpass
import hashlib
import http.cookies
import http.server
import json
import os
import random
import secrets
import socket
import sqlite3
import string
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# 端口：本地默认 8765；云端（Zeabur / Railway 等）通过 $PORT 注入
PORT = int(os.environ.get('PORT', 8765))
HTML_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'resume-coach.html',
)
LANDING_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'index.html',
)
CLAUDE_TIMEOUT_SECONDS = 600

# ---- 用户体系 / 数据库 ----
# 本地默认 ~/.resume-coach；云端通过 $DATA_DIR 指向挂载的持久卷 (e.g. /app/data)
DB_DIR = os.environ.get('DATA_DIR') or os.path.expanduser('~/.resume-coach')
DB_PATH = os.path.join(DB_DIR, 'app.db')
SESSION_COOKIE = 'session'
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 200_000
INVITE_CODE_LEN = 8
# 避免易混字符（0/O 1/I）
INVITE_CODE_ALPHABET = ''.join(c for c in string.ascii_uppercase + string.digits
                                if c not in 'O01I')
DEFAULT_INVITE_POINTS = 100
DEFAULT_INVITE_EXPIRE_DAYS = 30
# 不需要登录就能访问的路径
PUBLIC_PATHS = {'/', '/index.html', '/health', '/login', '/register', '/logout'}

# =============================================================================
# 数据库层
# =============================================================================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  password_salt TEXT NOT NULL,
  points_balance INTEGER NOT NULL DEFAULT 0,
  role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS invitation_codes (
  code TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'unused' CHECK(status IN ('unused','used','revoked','expired')),
  grant_points INTEGER NOT NULL DEFAULT 100,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  used_by_user_id INTEGER REFERENCES users(id),
  used_at TEXT,
  expires_at TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_invitation_status ON invitation_codes(status);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL,
  last_used_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS point_txns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL,
  ref_type TEXT,
  ref_id TEXT,
  balance_after INTEGER NOT NULL,
  operator_id INTEGER REFERENCES users(id),
  task_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_txn_user ON point_txns(user_id, created_at);

-- =============================================================
-- PR2: 任务体系
-- =============================================================
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL,
  target_role TEXT,
  target_direction TEXT,
  target_industry TEXT,
  salary_min INTEGER,
  salary_max INTEGER,
  location TEXT,
  remote_pref TEXT,
  note TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_active_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, status);

CREATE TABLE IF NOT EXISTS master_resumes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL,
  content_md TEXT,
  content_html TEXT,
  is_active INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'chat',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(task_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_master_resumes_task ON master_resumes(task_id);

CREATE TABLE IF NOT EXISTS company_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  company_name TEXT NOT NULL,
  position TEXT,
  jd_content TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','done','applied','interview','rejected','offered')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_company_targets_task ON company_targets(task_id);

CREATE TABLE IF NOT EXISTS company_critiques (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_target_id INTEGER NOT NULL UNIQUE REFERENCES company_targets(id) ON DELETE CASCADE,
  content_md TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tailored_resumes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_target_id INTEGER NOT NULL REFERENCES company_targets(id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL,
  content_md TEXT,
  content_html TEXT,
  is_active INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(company_target_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_tailored_target ON tailored_resumes(company_target_id);

CREATE TABLE IF NOT EXISTS interview_kits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_target_id INTEGER NOT NULL REFERENCES company_targets(id) ON DELETE CASCADE,
  kit_type TEXT NOT NULL CHECK(kit_type IN ('strategy','talking','qa','deep')),
  content_md TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(company_target_id, kit_type)
);
CREATE INDEX IF NOT EXISTS idx_kit_target ON interview_kits(company_target_id);

-- 任务的对话历史（Tab 1 chat 落库；CASCADE 删任务时一并删）
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
  content TEXT NOT NULL,
  attachments_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_task ON chat_messages(task_id, id);

-- =============================================================
-- PR3: 计费规则 + 充值码
-- =============================================================
CREATE TABLE IF NOT EXISTS billing_rules (
  action_key TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  points_cost INTEGER NOT NULL,
  category TEXT NOT NULL DEFAULT 'consume',  -- consume / save
  active INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_by INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS recharge_codes (
  code TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'unused' CHECK(status IN ('unused','used','revoked','expired')),
  grant_points INTEGER NOT NULL,
  bound_user_id INTEGER REFERENCES users(id),   -- NULL = 通用券，否则 = 实名券
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  used_by_user_id INTEGER REFERENCES users(id),
  used_at TEXT,
  expires_at TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_recharge_status ON recharge_codes(status);
CREATE INDEX IF NOT EXISTS idx_recharge_bound ON recharge_codes(bound_user_id);
"""

# 默认计费规则（init 时如不存在则注入）
DEFAULT_BILLING_RULES = [
    # 对话类
    ('chat_turn',                '对话追问（每轮）',           2,  'consume'),
    # 保存类（生成 + 落库）
    ('save_master_resume',       '主简历生成（最终输出）',     40, 'save'),
    ('save_critique',            '公司诊断报告',               15, 'save'),
    ('save_tailored_resume',     '公司特化简历',               15, 'save'),
    ('save_interview_strategy',  '面试战略分析',               15, 'save'),
    ('save_interview_talking',   '自我介绍口水稿',             10, 'save'),
    ('save_interview_qa',        '高频问答库',                 20, 'save'),
    ('save_interview_deep',      '深挖预案',                   20, 'save'),
]

# 默认充值档位（前端展示用，不强制落库；先放常量）
DEFAULT_RECHARGE_TIERS = [
    {'amount_yuan': 10,  'points_total': 100,  'points_bonus': 0},
    {'amount_yuan': 50,  'points_total': 550,  'points_bonus': 50},
    {'amount_yuan': 100, 'points_total': 1200, 'points_bonus': 200},
    {'amount_yuan': 300, 'points_total': 4000, 'points_bonus': 1000},
]


def get_db():
    """每个请求/CLI 命令拿一条新连接（SQLite WAL 模式下高并发够用）。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    """启动时调一次：建目录、建表、开 WAL、注入计费规则默认值。"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.executescript(SCHEMA_SQL)
        # 注入默认计费规则（仅当 action_key 不存在）
        for ak, label, cost, cat in DEFAULT_BILLING_RULES:
            conn.execute(
                '''INSERT OR IGNORE INTO billing_rules
                   (action_key, label, points_cost, category) VALUES (?,?,?,?)''',
                (ak, label, cost, cat),
            )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# PR3: 计费工具（预扣 / 退款 / 余额查询）
# =============================================================================
def get_billing_rule(conn, action_key):
    return conn.execute(
        'SELECT * FROM billing_rules WHERE action_key = ? AND active = 1',
        (action_key,),
    ).fetchone()


def precharge_and_record(conn, user_id, action_key, task_id=None,
                          ref_type=None, ref_id=None):
    """
    原子预扣 + 同时写一条 consume txn。返回 (txn_id, cost, error_code)
      error_code: None=成功 / 'no_rule' / 'insufficient'
    cost = 0 时算免费动作，不写 txn 直接返回 (None, 0, None)。
    """
    rule = get_billing_rule(conn, action_key)
    if not rule:
        return None, 0, 'no_rule'
    cost = int(rule['points_cost'])
    if cost <= 0:
        return None, 0, None

    cur = conn.execute(
        '''UPDATE users SET points_balance = points_balance - ?
           WHERE id = ? AND points_balance >= ?''',
        (cost, user_id, cost),
    )
    if cur.rowcount == 0:
        return None, cost, 'insufficient'

    new_balance = conn.execute(
        'SELECT points_balance FROM users WHERE id = ?', (user_id,)
    ).fetchone()['points_balance']

    cur2 = conn.execute(
        '''INSERT INTO point_txns
           (user_id, delta, reason, ref_type, ref_id, balance_after, task_id, operator_id)
           VALUES (?,?,?,?,?,?,?,?)''',
        (user_id, -cost, f'consume:{action_key}', ref_type, ref_id,
         new_balance, task_id, user_id),
    )
    return cur2.lastrowid, cost, None


def refund_by_txn(conn, txn_id, reason_suffix='auto'):
    """根据原 consume txn 退款：余额回补 + 写 refund txn。"""
    txn = conn.execute('SELECT * FROM point_txns WHERE id = ?', (txn_id,)).fetchone()
    if not txn or txn['delta'] >= 0:
        return False
    refund_amount = -txn['delta']
    conn.execute(
        'UPDATE users SET points_balance = points_balance + ? WHERE id = ?',
        (refund_amount, txn['user_id']),
    )
    new_balance = conn.execute(
        'SELECT points_balance FROM users WHERE id = ?', (txn['user_id'],),
    ).fetchone()['points_balance']
    conn.execute(
        '''INSERT INTO point_txns
           (user_id, delta, reason, ref_type, ref_id, balance_after, task_id, operator_id)
           VALUES (?,?,?,?,?,?,?,?)''',
        (txn['user_id'], refund_amount,
         f'refund:{txn["reason"]} ({reason_suffix})',
         'point_txn', str(txn_id), new_balance, txn['task_id'], txn['user_id']),
    )
    return True


def get_user_balance(conn, user_id):
    row = conn.execute('SELECT points_balance FROM users WHERE id = ?', (user_id,)).fetchone()
    return row['points_balance'] if row else 0


def charge_or_send_402(handler, user_id, action_key, task_id=None,
                       ref_type=None, ref_id=None):
    """
    便捷封装：handler 调用此函数完成预扣。
      返回 txn_id (int) 表示成功（含 None: 免费动作）
      返回 False 表示已写了 402 响应，handler 应直接 return
    """
    conn = get_db()
    try:
        txn_id, cost, err = precharge_and_record(
            conn, user_id, action_key, task_id, ref_type, ref_id,
        )
        if err == 'no_rule':
            conn.commit()
            handler._send_json(500, {'error': f'未定义的计费动作：{action_key}'})
            return False
        if err == 'insufficient':
            conn.commit()
            balance = get_user_balance(conn, user_id)
            rule = get_billing_rule(conn, action_key)
            handler._send_json(402, {
                'error': 'INSUFFICIENT_POINTS',
                'message': '积分不足',
                'cost': cost,
                'balance': balance,
                'action_key': action_key,
                'action_label': rule['label'] if rule else action_key,
            })
            return False
        conn.commit()
        # 成功（含 cost=0 免费动作时 txn_id=None）
        return txn_id if txn_id is not None else 0
    finally:
        conn.close()


def refund_in_new_conn(txn_id, reason_suffix='auto'):
    if not txn_id:
        return
    conn = get_db()
    try:
        refund_by_txn(conn, txn_id, reason_suffix)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Auth 工具
# =============================================================================
def hash_password(password: str, salt_hex: str = None) -> tuple:
    """返回 (salt_hex, hash_hex)。"""
    if salt_hex is None:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return salt_hex, dk.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    _, computed = hash_password(password, salt_hex)
    # 常量时间比较，防止 timing attack
    return secrets.compare_digest(computed, hash_hex)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def generate_invitation_code() -> str:
    return ''.join(random.choices(INVITE_CODE_ALPHABET, k=INVITE_CODE_LEN))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def utc_future_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')


def create_session(user_id: int) -> str:
    """创建新 session，返回 token。"""
    token = generate_session_token()
    expires_at = utc_future_iso(SESSION_DAYS)
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)',
            (token, user_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def validate_session(token: str):
    """返回 user row（dict）或 None。顺手刷新 last_used_at。"""
    if not token:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            '''SELECT u.* FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token = ?
                 AND s.expires_at > datetime('now')
                 AND u.status = 'active' ''',
            (token,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE sessions SET last_used_at = datetime('now') WHERE token = ?",
                (token,),
            )
            conn.commit()
            return dict(row)
        return None
    finally:
        conn.close()


def delete_session(token: str):
    if not token:
        return
    conn = get_db()
    try:
        conn.execute('DELETE FROM sessions WHERE token = ?', (token,))
        conn.commit()
    finally:
        conn.close()


def user_to_dict(row) -> dict:
    """把 user row 脱敏成前端可见结构。"""
    if not row:
        return None
    d = dict(row) if not isinstance(row, dict) else row
    return {
        'id': d['id'],
        'username': d['username'],
        'points_balance': d['points_balance'],
        'role': d['role'],
        'status': d['status'],
        'created_at': d['created_at'],
        'last_login_at': d.get('last_login_at'),
    }


# =============================================================================
# 任务体系辅助函数
# =============================================================================
def task_row_to_dict(row, extra=None):
    if not row:
        return None
    d = dict(row)
    if extra:
        d.update(extra)
    return d


def get_user_task(conn, task_id, user_id):
    """拿一条任务并校验属于当前用户。属于则返回 row，不属于返回 None。"""
    row = conn.execute(
        'SELECT * FROM tasks WHERE id = ? AND user_id = ?',
        (task_id, user_id),
    ).fetchone()
    return row


def get_user_company(conn, company_id, user_id):
    """拿一条 company_target 并通过 task 间接校验归属当前用户。"""
    row = conn.execute(
        '''SELECT ct.* , t.user_id AS owner_id
           FROM company_targets ct
           JOIN tasks t ON t.id = ct.task_id
           WHERE ct.id = ? AND t.user_id = ?''',
        (company_id, user_id),
    ).fetchone()
    return row


def touch_task(conn, task_id):
    conn.execute(
        "UPDATE tasks SET last_active_at = datetime('now') WHERE id = ?",
        (task_id,),
    )


def create_default_task(conn, user_id) -> int:
    """注册时调用，建一个默认任务，返回 task_id。"""
    cur = conn.execute(
        '''INSERT INTO tasks (user_id, title, status)
           VALUES (?, ?, 'active')''',
        (user_id, '我的求职方向'),
    )
    return cur.lastrowid


def next_master_version(conn, task_id) -> int:
    row = conn.execute(
        'SELECT COALESCE(MAX(version_no), 0) + 1 AS v FROM master_resumes WHERE task_id = ?',
        (task_id,),
    ).fetchone()
    return row['v']


def next_tailored_version(conn, company_target_id) -> int:
    row = conn.execute(
        'SELECT COALESCE(MAX(version_no), 0) + 1 AS v FROM tailored_resumes WHERE company_target_id = ?',
        (company_target_id,),
    ).fetchone()
    return row['v']


RATE_LIMIT_MARKERS = [
    "you've hit your limit",
    "you have hit your limit",
    "rate limit",
    "quota exceeded",
    "usage limit",
    "limit reached",
]


def detect_rate_limit(text: str) -> bool:
    if not text:
        return False
    lower = text.lower().strip()
    if len(lower) < 300 and any(m in lower for m in RATE_LIMIT_MARKERS):
        return True
    return False


def call_claude(system_prompt: str, user_message: str):
    cmd = ['claude', '-p', '--append-system-prompt', system_prompt]
    result = subprocess.run(
        cmd, input=user_message, capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    return result.stdout, result.stderr


def call_claude_chat(messages: list, system_prompt: str):
    if not messages:
        raise ValueError('messages 不能为空')
    if messages[-1].get('role') != 'user':
        raise ValueError('最后一条消息必须是 user 角色')

    if len(messages) == 1:
        full_input = messages[0]['content']
    else:
        history = messages[:-1]
        latest = messages[-1]['content']
        parts = []
        for i, m in enumerate(history, 1):
            role_cn = '用户' if m['role'] == 'user' else '助手（你）'
            parts.append(f'## 第 {i} 轮 — {role_cn}\n{m["content"]}')
        history_text = '\n\n'.join(parts)
        full_input = (
            '【已有对话历史】\n\n' + history_text + '\n\n---\n\n'
            '【用户最新消息】\n' + latest + '\n\n'
            '【请基于上述对话历史和 system prompt 中定义的工作流阶段，给出你的回复。'
            '注意：要正确判断当前应该处于哪个阶段（A 分析 / B 追问 / C Plan / D 生成简历），'
            '并按要求加上对应标签】'
        )

    cmd = ['claude', '-p', '--append-system-prompt', system_prompt]
    result = subprocess.run(
        cmd, input=full_input, capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    return result.stdout, result.stderr


class Handler(http.server.BaseHTTPRequestHandler):
    # ---- 容错：所有 socket 写入都吞掉断开/重置异常 ----
    def _safe_write(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _safe_send_response(self, status):
        try:
            self.send_response(status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _safe_send_header(self, key, value):
        try:
            self.send_header(key, value)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _safe_end_headers(self):
        try:
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    # 覆盖 finish 避免 wfile.flush 抛 BrokenPipe 没人接
    def finish(self):
        try:
            if not self.wfile.closed:
                try:
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                try:
                    self.wfile.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.rfile.close()
        except Exception:
            pass

    def _set_cors(self):
        self._safe_send_header('Access-Control-Allow-Origin', '*')
        self._safe_send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self._safe_send_header('Access-Control-Allow-Headers', 'Content-Type')

    # ---- Cookie / Session 工具 ----
    def _get_cookie(self, name: str):
        raw = self.headers.get('Cookie', '')
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
            morsel = jar.get(name)
            return morsel.value if morsel else None
        except http.cookies.CookieError:
            return None

    def _set_session_cookie(self, token: str):
        # HttpOnly + SameSite=Lax + Path=/，localhost 不设 Secure
        max_age = SESSION_DAYS * 24 * 3600
        cookie = (
            f'{SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; '
            f'HttpOnly; SameSite=Lax'
        )
        self._safe_send_header('Set-Cookie', cookie)

    def _clear_session_cookie(self):
        cookie = f'{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'
        self._safe_send_header('Set-Cookie', cookie)

    def _current_user(self):
        token = self._get_cookie(SESSION_COOKIE)
        return validate_session(token)

    def _require_auth(self):
        """已登录返回 user dict；未登录返回 None 并已经写好 401 响应。"""
        user = self._current_user()
        if user:
            return user
        self._send_json(401, {'error': 'UNAUTHENTICATED', 'message': '请先登录'})
        return None

    def _require_admin(self):
        """已登录且 role=admin 返回 user dict；否则写 401/403 并返回 None。"""
        user = self._require_auth()
        if not user:
            return None
        if user.get('role') != 'admin':
            self._send_json(403, {'error': 'FORBIDDEN', 'message': '需要管理员权限'})
            return None
        return user

    def _send_json(self, status: int, payload: dict):
        self._safe_send_response(status)
        self._set_cors()
        self._safe_send_header('Content-Type', 'application/json; charset=utf-8')
        self._safe_end_headers()
        self._safe_write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    def do_OPTIONS(self):
        self._safe_send_response(204)
        self._set_cors()
        self._safe_end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/health':
            self._send_json(200, {'ok': True})
            return

        if path == '/me':
            user = self._current_user()
            if not user:
                self._send_json(401, {'error': 'UNAUTHENTICATED'})
                return
            self._send_json(200, {'user': user_to_dict(user)})
            return

        # ---- PR4: admin 路由（先于普通路由判断）----
        if path.startswith('/admin/'):
            user = self._require_admin()
            if not user:
                return
            return self._dispatch_admin_get(path, user)

        # ---- PR2: 任务 / 简历 / 公司 GET 路由（都需要登录）----
        if path.startswith('/tasks') or path.startswith('/companies') \
           or path.startswith('/resumes') or path.startswith('/me/') \
           or path == '/me/wallet' or path == '/billing-rules':
            user = self._require_auth()
            if not user:
                return
            return self._dispatch_get_authed(path, user)

        # / 和 /index.html → landing page；/app 和 /app/ → 应用主体
        if path in ('/', '/index.html'):
            return self._serve_static_html(LANDING_FILE)
        if path in ('/app', '/app/', '/app/index.html'):
            return self._serve_static_html(HTML_FILE)

        self.send_error(404)

    def _serve_static_html(self, file_path):
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
        except FileNotFoundError:
            self.send_error(404, f'HTML 文件未找到：{file_path}')
            return
        self._safe_send_response(200)
        self._set_cors()
        self._safe_send_header('Content-Type', 'text/html; charset=utf-8')
        self._safe_send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self._safe_send_header('Pragma', 'no-cache')
        self._safe_send_header('Expires', '0')
        self._safe_end_headers()
        self._safe_write(content)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            # 公开路由（不需要登录）
            if path == '/register':
                return self._handle_register()
            if path == '/login':
                return self._handle_login()
            if path == '/logout':
                return self._handle_logout()

            # 受保护路由：必须已登录
            user = self._require_auth()
            if not user:
                return

            if path == '/generate':
                return self._handle_generate(user)
            if path == '/chat':
                return self._handle_chat(user)
            if path == '/upload-image':
                return self._handle_upload_image()
            if path == '/proxy-chat':
                return self._handle_proxy_chat(user)

            # PR2: 任务 / 公司 / 简历 / 面试包路由
            if path.startswith('/tasks') or path.startswith('/companies'):
                return self._dispatch_post_authed(path, user)
            # PR3: 充值码兑换
            if path == '/redeem':
                return self._dispatch_post_authed(path, user)
            # PR4: admin 路由（在 _require_auth 之外再做 admin 校验）
            if path.startswith('/admin/'):
                admin_user = self._require_admin()
                if not admin_user:
                    return
                return self._dispatch_admin_post(path, admin_user)

            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端中途断开，安静吞掉
            pass

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            user = self._require_auth()
            if not user:
                return
            if path.startswith('/tasks/') or path.startswith('/companies/'):
                return self._dispatch_delete_authed(path, user)
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_PATCH(self):
        path = urlparse(self.path).path
        try:
            if path.startswith('/admin/'):
                admin_user = self._require_admin()
                if not admin_user:
                    return
                return self._dispatch_admin_patch(path, admin_user)
            user = self._require_auth()
            if not user:
                return
            if path.startswith('/tasks/') or path.startswith('/companies/'):
                return self._dispatch_patch_authed(path, user)
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length).decode('utf-8')
        return json.loads(raw)

    # ===== 认证相关 =====
    def _handle_register(self):
        try:
            body = self._read_json_body()
            username = (body.get('username') or '').strip()
            password = body.get('password') or ''
            invite = (body.get('invitation_code') or '').strip().upper()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return

        # 字段校验
        if not (3 <= len(username) <= 30) or not all(c.isalnum() or c == '_' for c in username):
            self._send_json(400, {'error': '用户名 3-30 位，仅限字母/数字/下划线'})
            return
        if len(password) < 6:
            self._send_json(400, {'error': '密码至少 6 位'})
            return
        if not invite:
            self._send_json(400, {'error': '需要邀请码'})
            return

        conn = get_db()
        try:
            # 校验邀请码
            row = conn.execute(
                'SELECT * FROM invitation_codes WHERE code = ?', (invite,)
            ).fetchone()
            if not row:
                self._send_json(400, {'error': '邀请码不存在'})
                return
            if row['status'] != 'unused':
                self._send_json(400, {'error': f'邀请码已{("使用" if row["status"]=="used" else "失效")}'})
                return
            if row['expires_at']:
                exp = conn.execute(
                    "SELECT datetime(?) > datetime('now') AS ok", (row['expires_at'],)
                ).fetchone()
                if not exp['ok']:
                    conn.execute(
                        "UPDATE invitation_codes SET status='expired' WHERE code=?", (invite,)
                    )
                    conn.commit()
                    self._send_json(400, {'error': '邀请码已过期'})
                    return

            # 用户名唯一
            existing = conn.execute(
                'SELECT id FROM users WHERE username = ?', (username,)
            ).fetchone()
            if existing:
                self._send_json(400, {'error': '用户名已被占用'})
                return

            # 建用户
            salt_hex, hash_hex = hash_password(password)
            grant_points = int(row['grant_points'])
            cur = conn.execute(
                '''INSERT INTO users (username, password_hash, password_salt, points_balance)
                   VALUES (?,?,?,?)''',
                (username, hash_hex, salt_hex, grant_points),
            )
            user_id = cur.lastrowid

            # 写 point_txn（注册赠送）
            conn.execute(
                '''INSERT INTO point_txns (user_id, delta, reason, ref_type, ref_id, balance_after, operator_id)
                   VALUES (?,?,?,?,?,?,?)''',
                (user_id, grant_points, 'invitation_code_redeem',
                 'invitation_code', invite, grant_points, user_id),
            )

            # 标记邀请码使用
            conn.execute(
                '''UPDATE invitation_codes
                   SET status='used', used_by_user_id=?, used_at=datetime('now')
                   WHERE code=?''',
                (user_id, invite),
            )

            # 注：不再自动建默认任务。
            # 用户登录后必须手动建第一个任务才能用 AI 功能（前端 welcome flow）。

            conn.commit()

            user_row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        finally:
            conn.close()

        # 自动登录
        token = create_session(user_id)
        self._safe_send_response(200)
        self._set_cors()
        self._safe_send_header('Content-Type', 'application/json; charset=utf-8')
        self._set_session_cookie(token)
        self._safe_end_headers()
        self._safe_write(json.dumps(
            {'user': user_to_dict(user_row)},
            ensure_ascii=False,
        ).encode('utf-8'))

    def _handle_login(self):
        try:
            body = self._read_json_body()
            username = (body.get('username') or '').strip()
            password = body.get('password') or ''
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return

        if not username or not password:
            self._send_json(400, {'error': '需要用户名和密码'})
            return

        conn = get_db()
        try:
            row = conn.execute(
                'SELECT * FROM users WHERE username = ?', (username,)
            ).fetchone()
            if not row or not verify_password(password, row['password_salt'], row['password_hash']):
                self._send_json(401, {'error': '用户名或密码错误'})
                return
            if row['status'] != 'active':
                self._send_json(403, {'error': '账号已被禁用'})
                return
            conn.execute(
                "UPDATE users SET last_login_at = datetime('now') WHERE id=?",
                (row['id'],),
            )
            conn.commit()
            user_id = row['id']
            user_row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        finally:
            conn.close()

        token = create_session(user_id)
        self._safe_send_response(200)
        self._set_cors()
        self._safe_send_header('Content-Type', 'application/json; charset=utf-8')
        self._set_session_cookie(token)
        self._safe_end_headers()
        self._safe_write(json.dumps(
            {'user': user_to_dict(user_row)},
            ensure_ascii=False,
        ).encode('utf-8'))

    def _handle_logout(self):
        token = self._get_cookie(SESSION_COOKIE)
        delete_session(token)
        self._safe_send_response(200)
        self._set_cors()
        self._safe_send_header('Content-Type', 'application/json; charset=utf-8')
        self._clear_session_cookie()
        self._safe_end_headers()
        self._safe_write(b'{"ok":true}')

    # =========================================================================
    # PR2: 任务 / 公司 / 简历 / 面试包 路由分发
    # =========================================================================
    def _dispatch_get_authed(self, path, user):
        import re
        # /tasks
        if path == '/tasks':
            return self._task_list(user)
        # /tasks/<id>
        m = re.fullmatch(r'/tasks/(\d+)', path)
        if m:
            return self._task_detail(user, int(m.group(1)))
        # /tasks/<id>/resumes
        m = re.fullmatch(r'/tasks/(\d+)/resumes', path)
        if m:
            return self._master_resume_list(user, int(m.group(1)))
        # /tasks/<id>/companies
        m = re.fullmatch(r'/tasks/(\d+)/companies', path)
        if m:
            return self._company_list(user, int(m.group(1)))
        # /tasks/<id>/messages — 拉对话历史
        m = re.fullmatch(r'/tasks/(\d+)/messages', path)
        if m:
            return self._chat_messages_list(user, int(m.group(1)))
        # /resumes/<rid>
        m = re.fullmatch(r'/resumes/(\d+)', path)
        if m:
            return self._master_resume_detail(user, int(m.group(1)))
        # /companies/<cid>
        m = re.fullmatch(r'/companies/(\d+)', path)
        if m:
            return self._company_detail(user, int(m.group(1)))
        # /me/resumes
        if path == '/me/resumes':
            return self._me_all_resumes(user)
        # /me/companies
        if path == '/me/companies':
            return self._me_all_companies(user)
        # PR3: 钱包 / 计费规则
        if path == '/me/wallet':
            return self._me_wallet(user)
        if path == '/billing-rules':
            return self._billing_rules_list(user)
        self.send_error(404)

    def _dispatch_post_authed(self, path, user):
        import re
        if path == '/tasks':
            return self._task_create(user)
        m = re.fullmatch(r'/tasks/(\d+)/resumes', path)
        if m:
            return self._master_resume_save(user, int(m.group(1)))
        m = re.fullmatch(r'/tasks/(\d+)/resumes/(\d+)/activate', path)
        if m:
            return self._master_resume_activate(user, int(m.group(1)), int(m.group(2)))
        m = re.fullmatch(r'/tasks/(\d+)/companies', path)
        if m:
            return self._company_create(user, int(m.group(1)))
        # /tasks/<id>/messages — 追加对话消息
        m = re.fullmatch(r'/tasks/(\d+)/messages', path)
        if m:
            return self._chat_messages_append(user, int(m.group(1)))
        m = re.fullmatch(r'/companies/(\d+)/critique', path)
        if m:
            return self._critique_save(user, int(m.group(1)))
        m = re.fullmatch(r'/companies/(\d+)/tailored-resumes', path)
        if m:
            return self._tailored_save(user, int(m.group(1)))
        m = re.fullmatch(r'/companies/(\d+)/tailored-resumes/(\d+)/activate', path)
        if m:
            return self._tailored_activate(user, int(m.group(1)), int(m.group(2)))
        m = re.fullmatch(r'/companies/(\d+)/interview-kit', path)
        if m:
            return self._kit_save(user, int(m.group(1)))
        # PR3
        if path == '/redeem':
            return self._redeem_code(user)
        self.send_error(404)

    def _dispatch_patch_authed(self, path, user):
        import re
        m = re.fullmatch(r'/tasks/(\d+)', path)
        if m:
            return self._task_update(user, int(m.group(1)))
        m = re.fullmatch(r'/companies/(\d+)', path)
        if m:
            return self._company_update(user, int(m.group(1)))
        self.send_error(404)

    def _dispatch_delete_authed(self, path, user):
        import re
        m = re.fullmatch(r'/tasks/(\d+)', path)
        if m:
            return self._task_delete(user, int(m.group(1)))
        m = re.fullmatch(r'/companies/(\d+)', path)
        if m:
            return self._company_delete(user, int(m.group(1)))
        self.send_error(404)

    # ----- 任务 CRUD -----
    def _task_list(self, user):
        conn = get_db()
        try:
            rows = conn.execute(
                '''SELECT t.*,
                          (SELECT COUNT(*) FROM company_targets ct WHERE ct.task_id=t.id) AS company_count,
                          (SELECT MAX(version_no) FROM master_resumes WHERE task_id=t.id) AS master_version
                   FROM tasks t
                   WHERE t.user_id = ?
                   ORDER BY t.last_active_at DESC''',
                (user['id'],),
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {'tasks': [dict(r) for r in rows]})

    def _task_create(self, user):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        title = (body.get('title') or '').strip()
        if not title or len(title) > 100:
            self._send_json(400, {'error': '任务名称必填，不超过 100 字'})
            return
        clone_from = body.get('clone_from')
        fields = {
            'target_role': body.get('target_role'),
            'target_direction': body.get('target_direction'),
            'target_industry': body.get('target_industry'),
            'salary_min': body.get('salary_min'),
            'salary_max': body.get('salary_max'),
            'location': body.get('location'),
            'remote_pref': body.get('remote_pref'),
            'note': body.get('note'),
        }

        conn = get_db()
        try:
            cur = conn.execute(
                '''INSERT INTO tasks
                   (user_id, title, target_role, target_direction, target_industry,
                    salary_min, salary_max, location, remote_pref, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (user['id'], title,
                 fields['target_role'], fields['target_direction'], fields['target_industry'],
                 fields['salary_min'], fields['salary_max'],
                 fields['location'], fields['remote_pref'], fields['note']),
            )
            new_task_id = cur.lastrowid

            # 克隆主简历最新激活版（不克隆公司）
            if clone_from:
                src = conn.execute(
                    'SELECT * FROM tasks WHERE id = ? AND user_id = ?',
                    (clone_from, user['id']),
                ).fetchone()
                if src:
                    src_resume = conn.execute(
                        '''SELECT * FROM master_resumes
                           WHERE task_id = ? AND is_active = 1
                           ORDER BY version_no DESC LIMIT 1''',
                        (clone_from,),
                    ).fetchone()
                    if src_resume:
                        conn.execute(
                            '''INSERT INTO master_resumes
                               (task_id, version_no, content_md, content_html, is_active, source)
                               VALUES (?, 1, ?, ?, 1, 'clone')''',
                            (new_task_id, src_resume['content_md'], src_resume['content_html']),
                        )

            conn.commit()
            task_row = conn.execute('SELECT * FROM tasks WHERE id = ?', (new_task_id,)).fetchone()
        finally:
            conn.close()
        self._send_json(200, {'task': dict(task_row)})

    def _task_detail(self, user, task_id):
        conn = get_db()
        try:
            task = get_user_task(conn, task_id, user['id'])
            if not task:
                self._send_json(404, {'error': '任务不存在'})
                return
            resumes = conn.execute(
                '''SELECT id, version_no, is_active, source, created_at,
                          length(COALESCE(content_md,'')) AS md_len
                   FROM master_resumes WHERE task_id = ?
                   ORDER BY version_no DESC''',
                (task_id,),
            ).fetchall()
            companies = conn.execute(
                '''SELECT ct.*,
                          (SELECT COUNT(*) FROM tailored_resumes tr WHERE tr.company_target_id=ct.id) AS tr_count,
                          (SELECT COUNT(*) FROM interview_kits ik WHERE ik.company_target_id=ct.id) AS kit_count,
                          (SELECT 1 FROM company_critiques cc WHERE cc.company_target_id=ct.id) AS has_critique
                   FROM company_targets ct
                   WHERE ct.task_id = ? AND ct.archived = 0
                   ORDER BY ct.created_at DESC''',
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {
            'task': dict(task),
            'master_resume_versions': [dict(r) for r in resumes],
            'companies': [dict(r) for r in companies],
        })

    def _task_update(self, user, task_id):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        allowed = ['title', 'target_role', 'target_direction', 'target_industry',
                   'salary_min', 'salary_max', 'location', 'remote_pref', 'note', 'status']
        sets, vals = [], []
        for k in allowed:
            if k in body:
                sets.append(f'{k} = ?')
                vals.append(body[k])
        if not sets:
            self._send_json(400, {'error': '没有可更新字段'})
            return
        conn = get_db()
        try:
            t = get_user_task(conn, task_id, user['id'])
            if not t:
                self._send_json(404, {'error': '任务不存在'})
                return
            vals.append(task_id)
            conn.execute(f'UPDATE tasks SET {", ".join(sets)} WHERE id = ?', vals)
            conn.commit()
            row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        finally:
            conn.close()
        self._send_json(200, {'task': dict(row)})

    def _task_delete(self, user, task_id):
        """Q5: 硬删（ON DELETE CASCADE 自动清下游所有简历/公司/kit）。"""
        conn = get_db()
        try:
            t = get_user_task(conn, task_id, user['id'])
            if not t:
                self._send_json(404, {'error': '任务不存在'})
                return
            # 用户至少留一个任务（避免被清光后没地方落脚）
            others = conn.execute(
                'SELECT COUNT(*) AS n FROM tasks WHERE user_id = ?', (user['id'],)
            ).fetchone()['n']
            if others <= 1:
                self._send_json(400, {'error': '至少要保留一个任务，请先新建另一个再删此任务'})
                return
            conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
            conn.commit()
        finally:
            conn.close()
        self._send_json(200, {'ok': True})

    # ----- 主简历版本 -----
    def _master_resume_list(self, user, task_id):
        conn = get_db()
        try:
            t = get_user_task(conn, task_id, user['id'])
            if not t:
                self._send_json(404, {'error': '任务不存在'})
                return
            rows = conn.execute(
                '''SELECT id, version_no, is_active, source, created_at,
                          length(COALESCE(content_md,'')) AS md_len
                   FROM master_resumes
                   WHERE task_id = ?
                   ORDER BY version_no DESC''',
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {'resumes': [dict(r) for r in rows]})

    def _master_resume_save(self, user, task_id):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        md = body.get('content_md') or ''
        html = body.get('content_html')
        source = body.get('source') or 'chat'
        if not md:
            self._send_json(400, {'error': 'content_md 必填'})
            return
        # 任务归属校验
        conn = get_db()
        try:
            t = get_user_task(conn, task_id, user['id'])
            if not t:
                self._send_json(404, {'error': '任务不存在'})
                return
        finally:
            conn.close()
        # PR3: 预扣（克隆/手动保存可豁免）
        action_key = 'save_master_resume' if source == 'chat' else None
        txn_id = 0
        if action_key:
            txn_id = charge_or_send_402(self, user['id'], action_key, task_id)
            if txn_id is False:
                return
        # 真正写入
        try:
            conn = get_db()
            try:
                v = next_master_version(conn, task_id)
                conn.execute('UPDATE master_resumes SET is_active = 0 WHERE task_id = ?', (task_id,))
                cur = conn.execute(
                    '''INSERT INTO master_resumes
                       (task_id, version_no, content_md, content_html, is_active, source)
                       VALUES (?,?,?,?,1,?)''',
                    (task_id, v, md, html, source),
                )
                touch_task(conn, task_id)
                new_balance = get_user_balance(conn, user['id'])
                conn.commit()
                row = conn.execute('SELECT * FROM master_resumes WHERE id = ?', (cur.lastrowid,)).fetchone()
            finally:
                conn.close()
        except Exception as e:
            refund_in_new_conn(txn_id, 'save_failed')
            self._send_json(500, {'error': f'保存失败：{e}'})
            return
        self._send_json(200, {'resume': dict(row), 'balance': new_balance})

    def _master_resume_activate(self, user, task_id, rid):
        conn = get_db()
        try:
            t = get_user_task(conn, task_id, user['id'])
            if not t:
                self._send_json(404, {'error': '任务不存在'})
                return
            row = conn.execute(
                'SELECT * FROM master_resumes WHERE id = ? AND task_id = ?',
                (rid, task_id),
            ).fetchone()
            if not row:
                self._send_json(404, {'error': '简历版本不存在'})
                return
            conn.execute('UPDATE master_resumes SET is_active = 0 WHERE task_id = ?', (task_id,))
            conn.execute('UPDATE master_resumes SET is_active = 1 WHERE id = ?', (rid,))
            conn.commit()
        finally:
            conn.close()
        self._send_json(200, {'ok': True})

    def _master_resume_detail(self, user, rid):
        conn = get_db()
        try:
            row = conn.execute(
                '''SELECT mr.*, t.user_id AS owner_id, t.title AS task_title
                   FROM master_resumes mr
                   JOIN tasks t ON t.id = mr.task_id
                   WHERE mr.id = ?''',
                (rid,),
            ).fetchone()
            if not row or row['owner_id'] != user['id']:
                self._send_json(404, {'error': '简历不存在'})
                return
        finally:
            conn.close()
        self._send_json(200, {'resume': dict(row)})

    # ----- 公司目标 CRUD -----
    def _company_list(self, user, task_id):
        conn = get_db()
        try:
            t = get_user_task(conn, task_id, user['id'])
            if not t:
                self._send_json(404, {'error': '任务不存在'})
                return
            rows = conn.execute(
                '''SELECT ct.*,
                          (SELECT COUNT(*) FROM tailored_resumes WHERE company_target_id=ct.id) AS tr_count,
                          (SELECT COUNT(*) FROM interview_kits WHERE company_target_id=ct.id) AS kit_count,
                          (SELECT 1 FROM company_critiques WHERE company_target_id=ct.id) AS has_critique
                   FROM company_targets ct
                   WHERE ct.task_id = ? AND ct.archived = 0
                   ORDER BY ct.created_at DESC''',
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {'companies': [dict(r) for r in rows]})

    def _company_create(self, user, task_id):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        # 允许空 company_name（前端"加公司"先建空卡片，用户再填字段）
        # 真正诊断时前端会校验"公司/岗位/JD 三项必填"
        company_name = (body.get('company_name') or '').strip()
        conn = get_db()
        try:
            t = get_user_task(conn, task_id, user['id'])
            if not t:
                self._send_json(404, {'error': '任务不存在'})
                return
            cur = conn.execute(
                '''INSERT INTO company_targets
                   (task_id, company_name, position, jd_content)
                   VALUES (?,?,?,?)''',
                (task_id, company_name,
                 body.get('position'),
                 body.get('jd_content')),
            )
            touch_task(conn, task_id)
            conn.commit()
            row = conn.execute('SELECT * FROM company_targets WHERE id = ?', (cur.lastrowid,)).fetchone()
        finally:
            conn.close()
        self._send_json(200, {'company': dict(row)})

    def _company_detail(self, user, company_id):
        conn = get_db()
        try:
            ct = get_user_company(conn, company_id, user['id'])
            if not ct:
                self._send_json(404, {'error': '公司不存在'})
                return
            critique = conn.execute(
                'SELECT * FROM company_critiques WHERE company_target_id = ?',
                (company_id,),
            ).fetchone()
            tailored = conn.execute(
                '''SELECT id, version_no, is_active, content_md, content_html, created_at,
                          length(COALESCE(content_md,'')) AS md_len
                   FROM tailored_resumes WHERE company_target_id = ?
                   ORDER BY version_no DESC''',
                (company_id,),
            ).fetchall()
            kits = conn.execute(
                'SELECT * FROM interview_kits WHERE company_target_id = ?',
                (company_id,),
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {
            'company': dict(ct),
            'critique': dict(critique) if critique else None,
            'tailored_versions': [dict(r) for r in tailored],
            'interview_kits': [dict(r) for r in kits],
        })

    def _company_update(self, user, company_id):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        allowed = ['company_name', 'position', 'jd_content', 'status', 'archived']
        sets, vals = [], []
        for k in allowed:
            if k in body:
                sets.append(f'{k} = ?')
                vals.append(body[k])
        if not sets:
            self._send_json(400, {'error': '没有可更新字段'})
            return
        conn = get_db()
        try:
            ct = get_user_company(conn, company_id, user['id'])
            if not ct:
                self._send_json(404, {'error': '公司不存在'})
                return
            vals.append(company_id)
            conn.execute(f'UPDATE company_targets SET {", ".join(sets)} WHERE id = ?', vals)
            touch_task(conn, ct['task_id'])
            conn.commit()
            row = conn.execute('SELECT * FROM company_targets WHERE id = ?', (company_id,)).fetchone()
        finally:
            conn.close()
        self._send_json(200, {'company': dict(row)})

    def _company_delete(self, user, company_id):
        conn = get_db()
        try:
            ct = get_user_company(conn, company_id, user['id'])
            if not ct:
                self._send_json(404, {'error': '公司不存在'})
                return
            conn.execute('DELETE FROM company_targets WHERE id = ?', (company_id,))
            touch_task(conn, ct['task_id'])
            conn.commit()
        finally:
            conn.close()
        self._send_json(200, {'ok': True})

    # ----- 诊断 / 特化简历 / 面试包 -----
    def _critique_save(self, user, company_id):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        md = (body.get('content_md') or '').strip()
        if not md:
            self._send_json(400, {'error': 'content_md 必填'})
            return
        conn = get_db()
        try:
            ct = get_user_company(conn, company_id, user['id'])
            if not ct:
                self._send_json(404, {'error': '公司不存在'})
                return
            task_id_for_billing = ct['task_id']
        finally:
            conn.close()
        # PR3: 预扣
        txn_id = charge_or_send_402(self, user['id'], 'save_critique', task_id_for_billing,
                                     ref_type='company_target', ref_id=str(company_id))
        if txn_id is False:
            return
        try:
            conn = get_db()
            try:
                conn.execute(
                    '''INSERT INTO company_critiques (company_target_id, content_md)
                       VALUES (?,?)
                       ON CONFLICT(company_target_id) DO UPDATE SET
                         content_md=excluded.content_md,
                         updated_at=datetime('now')''',
                    (company_id, md),
                )
                touch_task(conn, task_id_for_billing)
                new_balance = get_user_balance(conn, user['id'])
                conn.commit()
                row = conn.execute(
                    'SELECT * FROM company_critiques WHERE company_target_id = ?', (company_id,)
                ).fetchone()
            finally:
                conn.close()
        except Exception as e:
            refund_in_new_conn(txn_id, 'save_failed')
            self._send_json(500, {'error': f'保存失败：{e}'})
            return
        self._send_json(200, {'critique': dict(row), 'balance': new_balance})

    def _tailored_save(self, user, company_id):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        md = body.get('content_md') or ''
        html = body.get('content_html')
        if not md:
            self._send_json(400, {'error': 'content_md 必填'})
            return
        conn = get_db()
        try:
            ct = get_user_company(conn, company_id, user['id'])
            if not ct:
                self._send_json(404, {'error': '公司不存在'})
                return
            task_id_for_billing = ct['task_id']
        finally:
            conn.close()
        txn_id = charge_or_send_402(self, user['id'], 'save_tailored_resume', task_id_for_billing,
                                     ref_type='company_target', ref_id=str(company_id))
        if txn_id is False:
            return
        try:
            conn = get_db()
            try:
                v = next_tailored_version(conn, company_id)
                conn.execute(
                    'UPDATE tailored_resumes SET is_active = 0 WHERE company_target_id = ?',
                    (company_id,),
                )
                cur = conn.execute(
                    '''INSERT INTO tailored_resumes
                       (company_target_id, version_no, content_md, content_html, is_active)
                       VALUES (?,?,?,?,1)''',
                    (company_id, v, md, html),
                )
                touch_task(conn, task_id_for_billing)
                new_balance = get_user_balance(conn, user['id'])
                conn.commit()
                row = conn.execute('SELECT * FROM tailored_resumes WHERE id = ?', (cur.lastrowid,)).fetchone()
            finally:
                conn.close()
        except Exception as e:
            refund_in_new_conn(txn_id, 'save_failed')
            self._send_json(500, {'error': f'保存失败：{e}'})
            return
        self._send_json(200, {'tailored_resume': dict(row), 'balance': new_balance})

    def _tailored_activate(self, user, company_id, rid):
        conn = get_db()
        try:
            ct = get_user_company(conn, company_id, user['id'])
            if not ct:
                self._send_json(404, {'error': '公司不存在'})
                return
            row = conn.execute(
                'SELECT * FROM tailored_resumes WHERE id = ? AND company_target_id = ?',
                (rid, company_id),
            ).fetchone()
            if not row:
                self._send_json(404, {'error': '版本不存在'})
                return
            conn.execute(
                'UPDATE tailored_resumes SET is_active = 0 WHERE company_target_id = ?',
                (company_id,),
            )
            conn.execute('UPDATE tailored_resumes SET is_active = 1 WHERE id = ?', (rid,))
            conn.commit()
        finally:
            conn.close()
        self._send_json(200, {'ok': True})

    def _kit_save(self, user, company_id):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        kit_type = (body.get('kit_type') or '').strip()
        md = (body.get('content_md') or '').strip()
        if kit_type not in ('strategy', 'talking', 'qa', 'deep'):
            self._send_json(400, {'error': 'kit_type 必须是 strategy/talking/qa/deep 之一'})
            return
        if not md:
            self._send_json(400, {'error': 'content_md 必填'})
            return
        conn = get_db()
        try:
            ct = get_user_company(conn, company_id, user['id'])
            if not ct:
                self._send_json(404, {'error': '公司不存在'})
                return
            task_id_for_billing = ct['task_id']
        finally:
            conn.close()
        action_key = f'save_interview_{kit_type}'
        txn_id = charge_or_send_402(self, user['id'], action_key, task_id_for_billing,
                                     ref_type='company_target', ref_id=str(company_id))
        if txn_id is False:
            return
        try:
            conn = get_db()
            try:
                conn.execute(
                    '''INSERT INTO interview_kits (company_target_id, kit_type, content_md)
                       VALUES (?,?,?)
                       ON CONFLICT(company_target_id, kit_type) DO UPDATE SET
                         content_md=excluded.content_md,
                         updated_at=datetime('now')''',
                    (company_id, kit_type, md),
                )
                touch_task(conn, task_id_for_billing)
                new_balance = get_user_balance(conn, user['id'])
                conn.commit()
                row = conn.execute(
                    'SELECT * FROM interview_kits WHERE company_target_id = ? AND kit_type = ?',
                    (company_id, kit_type),
                ).fetchone()
            finally:
                conn.close()
        except Exception as e:
            refund_in_new_conn(txn_id, 'save_failed')
            self._send_json(500, {'error': f'保存失败：{e}'})
            return
        self._send_json(200, {'kit': dict(row), 'balance': new_balance})

    # =========================================================================
    # PR4: 管理员后台
    # =========================================================================
    def _dispatch_admin_get(self, path, admin_user):
        import re
        if path == '/admin/dashboard':
            return self._admin_dashboard(admin_user)
        if path == '/admin/users':
            return self._admin_user_list(admin_user)
        m = re.fullmatch(r'/admin/users/(\d+)', path)
        if m:
            return self._admin_user_detail(admin_user, int(m.group(1)))
        if path == '/admin/invites':
            return self._admin_invite_list(admin_user)
        if path == '/admin/recharges':
            return self._admin_recharge_list(admin_user)
        self.send_error(404)

    def _dispatch_admin_post(self, path, admin_user):
        import re
        m = re.fullmatch(r'/admin/users/(\d+)/adjust', path)
        if m:
            return self._admin_user_adjust(admin_user, int(m.group(1)))
        m = re.fullmatch(r'/admin/users/(\d+)/reset-password', path)
        if m:
            return self._admin_user_reset_password(admin_user, int(m.group(1)))
        if path == '/admin/invites':
            return self._admin_invite_create(admin_user)
        m = re.fullmatch(r'/admin/invites/([A-Z0-9]+)/revoke', path)
        if m:
            return self._admin_invite_revoke(admin_user, m.group(1))
        if path == '/admin/recharges':
            return self._admin_recharge_create(admin_user)
        m = re.fullmatch(r'/admin/recharges/([A-Z0-9]+)/revoke', path)
        if m:
            return self._admin_recharge_revoke(admin_user, m.group(1))
        self.send_error(404)

    def _dispatch_admin_patch(self, path, admin_user):
        import re
        m = re.fullmatch(r'/admin/users/(\d+)', path)
        if m:
            return self._admin_user_update(admin_user, int(m.group(1)))
        self.send_error(404)

    # ----- 概览 -----
    def _admin_dashboard(self, admin_user):
        conn = get_db()
        try:
            stats = {}
            stats['users_total'] = conn.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']
            stats['users_today'] = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE date(created_at) = date('now','localtime')"
            ).fetchone()['n']
            stats['admins'] = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()['n']
            stats['balance_pool'] = conn.execute('SELECT COALESCE(SUM(points_balance),0) AS n FROM users').fetchone()['n']
            stats['consumed_total'] = conn.execute(
                "SELECT COALESCE(SUM(-delta),0) AS n FROM point_txns WHERE delta<0 AND reason LIKE 'consume:%'"
            ).fetchone()['n']
            stats['recharged_total'] = conn.execute(
                "SELECT COALESCE(SUM(delta),0) AS n FROM point_txns WHERE reason IN ('recharge_code_redeem','invitation_code_redeem')"
            ).fetchone()['n']
            stats['tasks_total'] = conn.execute('SELECT COUNT(*) AS n FROM tasks').fetchone()['n']
            stats['invites_unused'] = conn.execute("SELECT COUNT(*) AS n FROM invitation_codes WHERE status='unused'").fetchone()['n']
            stats['recharges_unused'] = conn.execute("SELECT COUNT(*) AS n FROM recharge_codes WHERE status='unused'").fetchone()['n']
            # 最近 10 笔大额消费/充值
            recent_txns = conn.execute(
                '''SELECT pt.*, u.username, t.title AS task_title
                   FROM point_txns pt
                   JOIN users u ON u.id = pt.user_id
                   LEFT JOIN tasks t ON t.id = pt.task_id
                   ORDER BY pt.id DESC LIMIT 15'''
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {
            'stats': stats,
            'recent_txns': [dict(r) for r in recent_txns],
        })

    # ----- 用户管理 -----
    def _admin_user_list(self, admin_user):
        from urllib.parse import parse_qs, urlparse as _urlparse
        qs = parse_qs(_urlparse(self.path).query)
        search = (qs.get('q', [''])[0] or '').strip()
        role_filter = qs.get('role', [''])[0]
        status_filter = qs.get('status', [''])[0]
        sql = '''SELECT u.id, u.username, u.role, u.status, u.points_balance,
                        u.created_at, u.last_login_at,
                        (SELECT COUNT(*) FROM tasks WHERE user_id=u.id) AS task_count
                 FROM users u
                 WHERE 1=1'''
        params = []
        if search:
            sql += ' AND u.username LIKE ?'
            params.append(f'%{search}%')
        if role_filter in ('user', 'admin'):
            sql += ' AND u.role = ?'
            params.append(role_filter)
        if status_filter in ('active', 'disabled'):
            sql += ' AND u.status = ?'
            params.append(status_filter)
        sql += ' ORDER BY u.id DESC LIMIT 200'
        conn = get_db()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        self._send_json(200, {'users': [dict(r) for r in rows]})

    def _admin_user_detail(self, admin_user, target_id):
        conn = get_db()
        try:
            u = conn.execute('SELECT * FROM users WHERE id = ?', (target_id,)).fetchone()
            if not u:
                self._send_json(404, {'error': '用户不存在'})
                return
            tasks = conn.execute(
                'SELECT id, title, target_role, status, created_at FROM tasks WHERE user_id = ? ORDER BY id DESC',
                (target_id,),
            ).fetchall()
            txns = conn.execute(
                '''SELECT pt.*, t.title AS task_title
                   FROM point_txns pt LEFT JOIN tasks t ON t.id = pt.task_id
                   WHERE pt.user_id = ? ORDER BY pt.id DESC LIMIT 50''',
                (target_id,),
            ).fetchall()
            recharges = conn.execute(
                '''SELECT code, grant_points, used_at, note FROM recharge_codes
                   WHERE used_by_user_id = ? ORDER BY used_at DESC''',
                (target_id,),
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {
            'user': user_to_dict(u),
            'tasks': [dict(r) for r in tasks],
            'recent_txns': [dict(r) for r in txns],
            'recharges': [dict(r) for r in recharges],
        })

    def _admin_user_update(self, admin_user, target_id):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        allowed = ['role', 'status']
        sets, vals = [], []
        for k in allowed:
            if k in body:
                if k == 'role' and body[k] not in ('user', 'admin'):
                    self._send_json(400, {'error': 'role 只能是 user / admin'})
                    return
                if k == 'status' and body[k] not in ('active', 'disabled'):
                    self._send_json(400, {'error': 'status 只能是 active / disabled'})
                    return
                sets.append(f'{k} = ?')
                vals.append(body[k])
        if not sets:
            self._send_json(400, {'error': '没有可更新字段'})
            return

        # 安全网：不允许把自己降级或禁用（避免锁死自己）
        if target_id == admin_user['id']:
            if 'role' in body and body['role'] != 'admin':
                self._send_json(400, {'error': '不能把自己降级为普通用户'})
                return
            if 'status' in body and body['status'] != 'active':
                self._send_json(400, {'error': '不能禁用自己'})
                return

        conn = get_db()
        try:
            t = conn.execute('SELECT * FROM users WHERE id = ?', (target_id,)).fetchone()
            if not t:
                self._send_json(404, {'error': '用户不存在'})
                return
            vals.append(target_id)
            conn.execute(f'UPDATE users SET {", ".join(sets)} WHERE id = ?', vals)
            # 如果禁用，把该用户所有 session 失效
            if 'status' in body and body['status'] == 'disabled':
                conn.execute('DELETE FROM sessions WHERE user_id = ?', (target_id,))
            conn.commit()
            row = conn.execute('SELECT * FROM users WHERE id = ?', (target_id,)).fetchone()
        finally:
            conn.close()
        self._send_json(200, {'user': user_to_dict(row)})

    def _admin_user_adjust(self, admin_user, target_id):
        """手动调整用户积分（带 reason）。delta 正负皆可。"""
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        try:
            delta = int(body.get('delta'))
        except (ValueError, TypeError):
            self._send_json(400, {'error': 'delta 必须是整数'})
            return
        reason = (body.get('reason') or '').strip()
        if not reason:
            self._send_json(400, {'error': '必须填写调整原因（用于审计）'})
            return
        if delta == 0:
            self._send_json(400, {'error': 'delta 不能为 0'})
            return

        conn = get_db()
        try:
            u = conn.execute('SELECT * FROM users WHERE id = ?', (target_id,)).fetchone()
            if not u:
                self._send_json(404, {'error': '用户不存在'})
                return
            # 负数调整不能把余额扣穿
            if delta < 0 and u['points_balance'] + delta < 0:
                self._send_json(400, {'error': f'扣减后余额为负（当前 {u["points_balance"]}）'})
                return
            conn.execute(
                'UPDATE users SET points_balance = points_balance + ? WHERE id = ?',
                (delta, target_id),
            )
            new_balance = get_user_balance(conn, target_id)
            conn.execute(
                '''INSERT INTO point_txns
                   (user_id, delta, reason, ref_type, ref_id, balance_after, operator_id)
                   VALUES (?,?,?,?,?,?,?)''',
                (target_id, delta, f'admin_adjust: {reason}',
                 'admin_user', str(admin_user['id']), new_balance, admin_user['id']),
            )
            conn.commit()
            row = conn.execute('SELECT * FROM users WHERE id = ?', (target_id,)).fetchone()
        finally:
            conn.close()
        self._send_json(200, {'user': user_to_dict(row), 'new_balance': new_balance})

    def _admin_user_reset_password(self, admin_user, target_id):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        new_pwd = body.get('new_password') or ''
        if len(new_pwd) < 6:
            self._send_json(400, {'error': '密码至少 6 位'})
            return
        salt_hex, hash_hex = hash_password(new_pwd)
        conn = get_db()
        try:
            u = conn.execute('SELECT * FROM users WHERE id = ?', (target_id,)).fetchone()
            if not u:
                self._send_json(404, {'error': '用户不存在'})
                return
            conn.execute(
                'UPDATE users SET password_hash=?, password_salt=? WHERE id=?',
                (hash_hex, salt_hex, target_id),
            )
            # 强制重新登录
            conn.execute('DELETE FROM sessions WHERE user_id = ?', (target_id,))
            conn.commit()
        finally:
            conn.close()
        self._send_json(200, {'ok': True})

    # ----- 邀请码 admin -----
    def _admin_invite_list(self, admin_user):
        from urllib.parse import parse_qs, urlparse as _urlparse
        qs = parse_qs(_urlparse(self.path).query)
        status_filter = qs.get('status', [''])[0]
        sql = '''SELECT ic.*, ub.username AS used_by_username
                 FROM invitation_codes ic
                 LEFT JOIN users ub ON ub.id = ic.used_by_user_id'''
        params = []
        if status_filter in ('unused', 'used', 'revoked', 'expired'):
            sql += ' WHERE ic.status = ?'
            params.append(status_filter)
        sql += ' ORDER BY ic.created_at DESC LIMIT 500'
        conn = get_db()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        self._send_json(200, {'invites': [dict(r) for r in rows]})

    def _admin_invite_create(self, admin_user):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        points = int(body.get('points', DEFAULT_INVITE_POINTS))
        count = int(body.get('count', 1))
        days = int(body.get('days', DEFAULT_INVITE_EXPIRE_DAYS))
        note = (body.get('note') or '').strip() or None
        if count < 1 or count > 200:
            self._send_json(400, {'error': '一次最多生成 200 张'})
            return
        if points < 0:
            self._send_json(400, {'error': '积分不能为负'})
            return
        conn = get_db()
        codes = []
        try:
            for _ in range(count):
                for _attempt in range(10):
                    code = generate_invitation_code()
                    try:
                        conn.execute(
                            '''INSERT INTO invitation_codes
                               (code, grant_points, expires_at, note, created_by)
                               VALUES (?,?,?,?,?)''',
                            (code, points, utc_future_iso(days), note, admin_user['id']),
                        )
                        codes.append(code)
                        break
                    except sqlite3.IntegrityError:
                        continue
            conn.commit()
        finally:
            conn.close()
        self._send_json(200, {'codes': codes, 'points': points, 'days': days})

    def _admin_invite_revoke(self, admin_user, code):
        conn = get_db()
        try:
            row = conn.execute('SELECT * FROM invitation_codes WHERE code = ?', (code,)).fetchone()
            if not row:
                self._send_json(404, {'error': '邀请码不存在'})
                return
            if row['status'] != 'unused':
                self._send_json(400, {'error': f'当前状态 {row["status"]}，不可撤销'})
                return
            conn.execute("UPDATE invitation_codes SET status='revoked' WHERE code = ?", (code,))
            conn.commit()
        finally:
            conn.close()
        self._send_json(200, {'ok': True})

    # ----- 充值码 admin -----
    def _admin_recharge_list(self, admin_user):
        from urllib.parse import parse_qs, urlparse as _urlparse
        qs = parse_qs(_urlparse(self.path).query)
        status_filter = qs.get('status', [''])[0]
        sql = '''SELECT rc.*,
                        ub.username AS bound_username,
                        uu.username AS used_by_username
                 FROM recharge_codes rc
                 LEFT JOIN users ub ON ub.id = rc.bound_user_id
                 LEFT JOIN users uu ON uu.id = rc.used_by_user_id'''
        params = []
        if status_filter in ('unused', 'used', 'revoked', 'expired'):
            sql += ' WHERE rc.status = ?'
            params.append(status_filter)
        sql += ' ORDER BY rc.created_at DESC LIMIT 500'
        conn = get_db()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        self._send_json(200, {'recharges': [dict(r) for r in rows]})

    def _admin_recharge_create(self, admin_user):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        try:
            points = int(body.get('points'))
        except (ValueError, TypeError):
            self._send_json(400, {'error': 'points 必填且为整数'})
            return
        count = int(body.get('count', 1))
        days = int(body.get('days', 60))
        note = (body.get('note') or '').strip() or None
        bound_id = body.get('bound_user_id')  # 可选（实名券）
        bound_username = (body.get('bound_username') or '').strip()  # 也支持用户名

        if points <= 0:
            self._send_json(400, {'error': 'points 必须大于 0'})
            return
        if count < 1 or count > 200:
            self._send_json(400, {'error': '一次最多生成 200 张'})
            return

        # 解析 bound user
        bound_user_id = None
        if bound_id is not None:
            try:
                bound_user_id = int(bound_id)
            except (ValueError, TypeError):
                self._send_json(400, {'error': 'bound_user_id 必须是整数'})
                return
        elif bound_username:
            conn = get_db()
            try:
                u = conn.execute('SELECT id FROM users WHERE username = ?', (bound_username,)).fetchone()
            finally:
                conn.close()
            if not u:
                self._send_json(404, {'error': f'用户 {bound_username} 不存在'})
                return
            bound_user_id = u['id']

        if bound_user_id is not None:
            conn = get_db()
            try:
                check = conn.execute('SELECT id FROM users WHERE id = ?', (bound_user_id,)).fetchone()
            finally:
                conn.close()
            if not check:
                self._send_json(404, {'error': f'user_id {bound_user_id} 不存在'})
                return

        conn = get_db()
        codes = []
        try:
            for _ in range(count):
                for _attempt in range(10):
                    code = generate_invitation_code()
                    try:
                        conn.execute(
                            '''INSERT INTO recharge_codes
                               (code, grant_points, bound_user_id, expires_at, note, created_by)
                               VALUES (?,?,?,?,?,?)''',
                            (code, points, bound_user_id, utc_future_iso(days), note, admin_user['id']),
                        )
                        codes.append(code)
                        break
                    except sqlite3.IntegrityError:
                        continue
            conn.commit()
        finally:
            conn.close()
        kind = 'bound' if bound_user_id else 'universal'
        self._send_json(200, {
            'codes': codes, 'points': points, 'days': days,
            'kind': kind, 'bound_user_id': bound_user_id,
        })

    def _admin_recharge_revoke(self, admin_user, code):
        conn = get_db()
        try:
            row = conn.execute('SELECT * FROM recharge_codes WHERE code = ?', (code,)).fetchone()
            if not row:
                self._send_json(404, {'error': '充值码不存在'})
                return
            if row['status'] != 'unused':
                self._send_json(400, {'error': f'当前状态 {row["status"]}，不可撤销'})
                return
            conn.execute("UPDATE recharge_codes SET status='revoked' WHERE code = ?", (code,))
            conn.commit()
        finally:
            conn.close()
        self._send_json(200, {'ok': True})

    # ----- 任务的对话历史（不计费，跟着 chat_turn 一起跑）-----
    def _chat_messages_list(self, user, task_id):
        conn = get_db()
        try:
            t = get_user_task(conn, task_id, user['id'])
            if not t:
                self._send_json(404, {'error': '任务不存在'})
                return
            rows = conn.execute(
                '''SELECT id, role, content, attachments_json, created_at
                   FROM chat_messages WHERE task_id = ?
                   ORDER BY id ASC''',
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        # 解析 attachments_json
        out = []
        for r in rows:
            d = dict(r)
            if d.get('attachments_json'):
                try: d['attachments'] = json.loads(d['attachments_json'])
                except Exception: d['attachments'] = None
            else:
                d['attachments'] = None
            d.pop('attachments_json', None)
            out.append(d)
        self._send_json(200, {'messages': out})

    def _chat_messages_append(self, user, task_id):
        """前端一次性 POST 一批新消息（通常是 1 条 user + 1 条 assistant）。"""
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        messages = body.get('messages', [])
        if not isinstance(messages, list) or not messages:
            self._send_json(400, {'error': 'messages 必须是非空数组'})
            return

        conn = get_db()
        try:
            t = get_user_task(conn, task_id, user['id'])
            if not t:
                self._send_json(404, {'error': '任务不存在'})
                return
            inserted = []
            for m in messages:
                role = (m.get('role') or '').strip()
                content = m.get('content') or ''
                if role not in ('user', 'assistant', 'system') or not content:
                    continue
                att = m.get('attachments')
                att_json = json.dumps(att, ensure_ascii=False) if att else None
                cur = conn.execute(
                    '''INSERT INTO chat_messages (task_id, role, content, attachments_json)
                       VALUES (?,?,?,?)''',
                    (task_id, role, content, att_json),
                )
                inserted.append(cur.lastrowid)
            touch_task(conn, task_id)
            conn.commit()
        finally:
            conn.close()
        self._send_json(200, {'ok': True, 'inserted_ids': inserted})

    # ----- PR3: 钱包 / 计费 / 充值码 -----
    def _billing_rules_list(self, user):
        conn = get_db()
        try:
            rows = conn.execute(
                'SELECT action_key, label, points_cost, category, active FROM billing_rules ORDER BY category, points_cost'
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {
            'rules': [dict(r) for r in rows],
            'recharge_tiers': DEFAULT_RECHARGE_TIERS,
        })

    def _me_wallet(self, user):
        """返回：余额 / 消费明细（含 task 信息）/ 充值记录 / 按任务聚合的消费汇总。"""
        conn = get_db()
        try:
            balance = get_user_balance(conn, user['id'])
            txns = conn.execute(
                '''SELECT pt.*, t.title AS task_title
                   FROM point_txns pt
                   LEFT JOIN tasks t ON t.id = pt.task_id
                   WHERE pt.user_id = ?
                   ORDER BY pt.id DESC
                   LIMIT 200''',
                (user['id'],),
            ).fetchall()
            recharges = conn.execute(
                '''SELECT code, grant_points, used_at, note
                   FROM recharge_codes
                   WHERE used_by_user_id = ?
                   ORDER BY used_at DESC''',
                (user['id'],),
            ).fetchall()
            # 按任务聚合 consume
            by_task = conn.execute(
                '''SELECT pt.task_id, t.title AS task_title,
                          SUM(CASE WHEN pt.delta < 0 THEN -pt.delta ELSE 0 END) AS spent,
                          SUM(CASE WHEN pt.delta > 0 AND pt.reason LIKE 'refund:%' THEN pt.delta ELSE 0 END) AS refunded,
                          COUNT(*) AS txn_count
                   FROM point_txns pt
                   LEFT JOIN tasks t ON t.id = pt.task_id
                   WHERE pt.user_id = ?
                   GROUP BY pt.task_id, t.title
                   ORDER BY spent DESC''',
                (user['id'],),
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {
            'balance': balance,
            'transactions': [dict(r) for r in txns],
            'recharges': [dict(r) for r in recharges],
            'by_task': [dict(r) for r in by_task],
        })

    def _redeem_code(self, user):
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        code = (body.get('code') or '').strip().upper()
        if not code:
            self._send_json(400, {'error': '请输入兑换码'})
            return

        conn = get_db()
        try:
            row = conn.execute(
                'SELECT * FROM recharge_codes WHERE code = ?', (code,)
            ).fetchone()
            if not row:
                self._send_json(404, {'error': '兑换码不存在'})
                return
            if row['status'] != 'unused':
                self._send_json(400, {'error': f'兑换码已{ {"used":"使用","revoked":"撤销","expired":"过期"}.get(row["status"], "失效") }'})
                return
            if row['expires_at']:
                exp = conn.execute(
                    "SELECT datetime(?) > datetime('now') AS ok", (row['expires_at'],)
                ).fetchone()
                if not exp['ok']:
                    conn.execute("UPDATE recharge_codes SET status='expired' WHERE code=?", (code,))
                    conn.commit()
                    self._send_json(400, {'error': '兑换码已过期'})
                    return
            if row['bound_user_id'] and row['bound_user_id'] != user['id']:
                self._send_json(403, {'error': '此兑换码绑定其他用户，无法使用'})
                return

            grant = int(row['grant_points'])
            # 加余额 + 记 txn + 标记 used
            conn.execute(
                'UPDATE users SET points_balance = points_balance + ? WHERE id = ?',
                (grant, user['id']),
            )
            new_balance = get_user_balance(conn, user['id'])
            conn.execute(
                '''INSERT INTO point_txns
                   (user_id, delta, reason, ref_type, ref_id, balance_after, operator_id)
                   VALUES (?,?,?,?,?,?,?)''',
                (user['id'], grant, 'recharge_code_redeem', 'recharge_code', code,
                 new_balance, user['id']),
            )
            conn.execute(
                '''UPDATE recharge_codes
                   SET status='used', used_by_user_id=?, used_at=datetime('now')
                   WHERE code=?''',
                (user['id'], code),
            )
            conn.commit()
        finally:
            conn.close()

        self._send_json(200, {
            'ok': True,
            'granted': grant,
            'balance': new_balance,
            'message': f'已到账 {grant} 积分',
        })

    # ----- 个人中心跨任务视图 -----
    def _me_all_resumes(self, user):
        """返回当前用户所有简历（主简历 + 特化简历），按 created_at 倒序。"""
        conn = get_db()
        try:
            master_rows = conn.execute(
                '''SELECT mr.id, mr.task_id, mr.version_no, mr.is_active, mr.source, mr.created_at,
                          length(COALESCE(mr.content_md,'')) AS md_len,
                          t.title AS task_title, 'master' AS kind
                   FROM master_resumes mr
                   JOIN tasks t ON t.id = mr.task_id
                   WHERE t.user_id = ?''',
                (user['id'],),
            ).fetchall()
            tailored_rows = conn.execute(
                '''SELECT tr.id, ct.task_id, tr.version_no, tr.is_active, NULL AS source, tr.created_at,
                          length(COALESCE(tr.content_md,'')) AS md_len,
                          t.title AS task_title, 'tailored' AS kind,
                          ct.id AS company_target_id, ct.company_name, ct.position
                   FROM tailored_resumes tr
                   JOIN company_targets ct ON ct.id = tr.company_target_id
                   JOIN tasks t ON t.id = ct.task_id
                   WHERE t.user_id = ?''',
                (user['id'],),
            ).fetchall()
        finally:
            conn.close()
        all_rows = [dict(r) for r in master_rows] + [dict(r) for r in tailored_rows]
        all_rows.sort(key=lambda r: r['created_at'], reverse=True)
        self._send_json(200, {'resumes': all_rows})

    def _me_all_companies(self, user):
        conn = get_db()
        try:
            rows = conn.execute(
                '''SELECT ct.*, t.title AS task_title
                   FROM company_targets ct
                   JOIN tasks t ON t.id = ct.task_id
                   WHERE t.user_id = ?
                   ORDER BY ct.created_at DESC''',
                (user['id'],),
            ).fetchall()
        finally:
            conn.close()
        self._send_json(200, {'companies': [dict(r) for r in rows]})

    # ===== 业务接口 =====
    def _handle_generate(self, current_user):
        try:
            body = self._read_json_body()
            system_prompt = body.get('system', '').strip()
            user_message = body.get('user', '').strip()
            task_id = body.get('task_id')  # PR2: 可选，未来 PR3 会变必填
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        if not user_message:
            self._send_json(400, {'error': '缺少 user 字段'})
            return

        # PR2: 如果带了 task_id，校验归属并刷新 last_active_at
        if task_id is not None:
            conn = get_db()
            try:
                t = get_user_task(conn, task_id, current_user['id'])
                if not t:
                    self._send_json(403, {'error': '任务不存在或无权访问'})
                    return
                touch_task(conn, task_id)
                conn.commit()
            finally:
                conn.close()

        # PR3: 预扣 chat_turn 积分
        txn_id = charge_or_send_402(self, current_user['id'], 'chat_turn', task_id)
        if txn_id is False:
            return

        sys.stderr.write(
            f'[{time.strftime("%H:%M:%S")}] → /generate '
            f'(sys={len(system_prompt)} usr={len(user_message)})\n'
        )
        sys.stderr.flush()
        start = time.time()
        try:
            stdout, stderr = call_claude(system_prompt, user_message)
        except subprocess.TimeoutExpired:
            refund_in_new_conn(txn_id, 'claude_timeout')
            self._send_json(504, {'error': f'Claude 超时（>{CLAUDE_TIMEOUT_SECONDS}s）'})
            return
        except FileNotFoundError:
            refund_in_new_conn(txn_id, 'claude_not_found')
            self._send_json(500, {'error': '找不到 claude CLI'})
            return
        except Exception as e:
            refund_in_new_conn(txn_id, 'exception')
            self._send_json(500, {'error': f'{e}'})
            return

        elapsed = time.time() - start
        sys.stderr.write(f'[{time.strftime("%H:%M:%S")}] ← /generate done {elapsed:.1f}s ({len(stdout)} chars)\n')
        sys.stderr.flush()
        if detect_rate_limit(stdout):
            refund_in_new_conn(txn_id, 'rate_limit')
            self._send_json(429, {'error': 'RATE_LIMIT', 'message': stdout.strip()})
            return
        # 成功：把当前余额一起返回，便于前端 header 实时更新
        new_balance = None
        if txn_id:
            conn = get_db()
            try:
                new_balance = get_user_balance(conn, current_user['id'])
            finally:
                conn.close()
        self._send_json(200, {'text': stdout, 'stderr': stderr, 'balance': new_balance})

    def _handle_chat(self, current_user):
        try:
            body = self._read_json_body()
            messages = body.get('messages', [])
            system_prompt = body.get('system', '').strip()
            task_id = body.get('task_id')
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        if not isinstance(messages, list) or not messages:
            self._send_json(400, {'error': 'messages 必须是非空数组'})
            return

        if task_id is not None:
            conn = get_db()
            try:
                t = get_user_task(conn, task_id, current_user['id'])
                if not t:
                    self._send_json(403, {'error': '任务不存在或无权访问'})
                    return
                touch_task(conn, task_id)
                conn.commit()
            finally:
                conn.close()

        # PR3: 预扣 chat_turn 积分
        txn_id = charge_or_send_402(self, current_user['id'], 'chat_turn', task_id)
        if txn_id is False:
            return

        sys.stderr.write(
            f'[{time.strftime("%H:%M:%S")}] → /chat '
            f'(轮={len(messages)} sys={len(system_prompt)})\n'
        )
        sys.stderr.flush()
        start = time.time()
        try:
            stdout, stderr = call_claude_chat(messages, system_prompt)
        except subprocess.TimeoutExpired:
            refund_in_new_conn(txn_id, 'claude_timeout')
            self._send_json(504, {'error': f'Claude 超时（>{CLAUDE_TIMEOUT_SECONDS}s）'})
            return
        except FileNotFoundError:
            refund_in_new_conn(txn_id, 'claude_not_found')
            self._send_json(500, {'error': '找不到 claude CLI'})
            return
        except ValueError as e:
            refund_in_new_conn(txn_id, 'value_error')
            self._send_json(400, {'error': str(e)})
            return
        except Exception as e:
            refund_in_new_conn(txn_id, 'exception')
            self._send_json(500, {'error': f'{e}'})
            return

        elapsed = time.time() - start
        sys.stderr.write(f'[{time.strftime("%H:%M:%S")}] ← /chat done {elapsed:.1f}s ({len(stdout)} chars)\n')
        sys.stderr.flush()
        if detect_rate_limit(stdout):
            refund_in_new_conn(txn_id, 'rate_limit')
            self._send_json(429, {'error': 'RATE_LIMIT', 'message': stdout.strip()})
            return
        new_balance = None
        if txn_id:
            conn = get_db()
            try:
                new_balance = get_user_balance(conn, current_user['id'])
            finally:
                conn.close()
        self._send_json(200, {'text': stdout, 'stderr': stderr, 'balance': new_balance})

    def _handle_proxy_chat(self, current_user):
        """
        转发到 OpenAI-compatible API（用户在前端配置的模型）。
        请求体：{baseUrl, apiKey, modelName, system, user, messages, temperature?, task_id?}
        """
        import urllib.request
        import urllib.error
        try:
            body = self._read_json_body()
            base_url = (body.get('baseUrl') or '').strip()
            api_key = (body.get('apiKey') or '').strip()
            model_name = (body.get('modelName') or '').strip()
            system_prompt = (body.get('system') or '').strip()
            user_msg = (body.get('user') or '').strip()
            messages_in = body.get('messages') or []
            temperature = body.get('temperature', 0.7)
            task_id = body.get('task_id')
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return

        if task_id is not None:
            conn = get_db()
            try:
                t = get_user_task(conn, task_id, current_user['id'])
                if not t:
                    self._send_json(403, {'error': '任务不存在或无权访问'})
                    return
                touch_task(conn, task_id)
                conn.commit()
            finally:
                conn.close()

        if not base_url or not api_key or not model_name:
            self._send_json(400, {'error': '缺少 baseUrl / apiKey / modelName（请在「模型设置」里检查）'})
            return

        # PR3: 预扣（在校验完 baseUrl/apiKey 后再扣，避免提前扣）
        txn_id = charge_or_send_402(self, current_user['id'], 'chat_turn', task_id)
        if txn_id is False:
            return

        # 拼接 OpenAI 风格的 messages
        formatted = []
        if system_prompt:
            formatted.append({'role': 'system', 'content': system_prompt})
        if messages_in:
            for m in messages_in:
                role = m.get('role')
                content = m.get('content', '')
                if role in ('user', 'assistant') and content:
                    formatted.append({'role': role, 'content': content})
        elif user_msg:
            formatted.append({'role': 'user', 'content': user_msg})
        else:
            self._send_json(400, {'error': '需要 user 字段或 messages 数组'})
            return

        # 规范化 URL：如果用户给的 baseUrl 没带 /chat/completions 就补上
        url = base_url.rstrip('/')
        if not url.endswith('/chat/completions'):
            # 常见：用户填 https://api.openai.com/v1 → 拼成 .../v1/chat/completions
            url = url + '/chat/completions'

        payload = {
            'model': model_name,
            'messages': formatted,
            'temperature': temperature,
            'stream': False,
        }
        req_data = json.dumps(payload).encode('utf-8')

        sys.stderr.write(
            f'[{time.strftime("%H:%M:%S")}] → /proxy-chat → {url} '
            f'(model={model_name}, msgs={len(formatted)})\n'
        )
        sys.stderr.flush()
        start = time.time()

        try:
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                },
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=CLAUDE_TIMEOUT_SECONDS) as resp:
                resp_body = resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            err_text = ''
            try:
                err_text = e.read().decode('utf-8', errors='replace')[:800]
            except Exception:
                pass
            sys.stderr.write(f'  ❌ HTTP {e.code}: {err_text[:200]}\n')
            refund_in_new_conn(txn_id, f'upstream_{e.code}')
            self._send_json(e.code if e.code < 600 else 500, {'error': f'API 返回 {e.code}：{err_text or e.reason}'})
            return
        except urllib.error.URLError as e:
            sys.stderr.write(f'  ❌ URLError: {e.reason}\n')
            refund_in_new_conn(txn_id, 'url_error')
            self._send_json(502, {'error': f'连不上 {url}：{e.reason}'})
            return
        except Exception as e:
            sys.stderr.write(f'  ❌ {type(e).__name__}: {e}\n')
            refund_in_new_conn(txn_id, 'exception')
            self._send_json(500, {'error': f'代理请求失败：{e}'})
            return

        elapsed = time.time() - start
        try:
            resp_json = json.loads(resp_body)
            text = resp_json['choices'][0]['message']['content']
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            sys.stderr.write(f'  ❌ 响应解析失败 ({e})：{resp_body[:300]}\n')
            refund_in_new_conn(txn_id, 'parse_error')
            self._send_json(500, {'error': f'响应不符合 OpenAI 格式：{resp_body[:300]}'})
            return

        sys.stderr.write(
            f'[{time.strftime("%H:%M:%S")}] ← /proxy-chat done {elapsed:.1f}s ({len(text)} chars)\n'
        )
        sys.stderr.flush()
        new_balance = None
        if txn_id:
            conn = get_db()
            try:
                new_balance = get_user_balance(conn, current_user['id'])
            finally:
                conn.close()
        self._send_json(200, {'text': text, 'balance': new_balance})

    def _handle_upload_image(self):
        import base64
        try:
            body = self._read_json_body()
            data_url = body.get('dataUrl', '')
            filename_hint = body.get('filename', 'image')
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {'error': f'请求格式错误：{e}'})
            return
        if ';base64,' not in data_url:
            self._send_json(400, {'error': '需要 base64 data URL'})
            return
        try:
            mime, b64 = data_url.split(';base64,', 1)
            ext = mime.split('/')[-1].lower() if '/' in mime else 'png'
            if ext not in ('png', 'jpg', 'jpeg', 'webp', 'gif'):
                ext = 'png'
            img_bytes = base64.b64decode(b64)
        except Exception as e:
            self._send_json(400, {'error': f'解码失败：{e}'})
            return

        tmp_dir = '/tmp/resume-coach-uploads'
        os.makedirs(tmp_dir, exist_ok=True)
        fname = f'{int(time.time() * 1000)}-{filename_hint[:20].replace("/", "_")}.{ext}'
        path = os.path.join(tmp_dir, fname)
        with open(path, 'wb') as f:
            f.write(img_bytes)
        self._send_json(200, {'path': path, 'size': len(img_bytes)})

    def log_message(self, fmt, *args):
        # 静默 access log
        pass

    def handle_one_request(self):
        # 吞掉 connection-level 异常，避免一个崩溃 handler 导致进程异常
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True


# ---- ThreadingHTTPServer 并发处理 ----
class ResumeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True       # 让所有 worker 线程是 daemon，主进程退出时一并清理
    allow_reuse_address = True  # 端口立即可重用，避免 SO_REUSEADDR 问题

    def handle_error(self, request, client_address):
        # 默认会打印 traceback 到 stderr — 我们静默吞掉 broken pipe 等噪声
        exc_type, exc, tb = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        sys.stderr.write(f'[server error] {exc_type.__name__}: {exc}\n')


def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except (ConnectionRefusedError, socket.timeout):
        return False
    finally:
        s.close()


def open_browser_after_delay():
    # 云端环境（$PORT 被注入）跑在 headless 容器里，不要尝试打开浏览器
    if os.environ.get('PORT'):
        return
    time.sleep(0.6)
    try:
        webbrowser.open(f'http://localhost:{PORT}/')
    except Exception:
        pass


# =============================================================================
# CLI 子命令（admin create / admin list / invite create）
# =============================================================================
def cli_admin_create(args):
    """python3 resume-coach-server.py admin create [username]"""
    init_db()
    username = args[0] if args else input('用户名: ').strip()
    if not (3 <= len(username) <= 30) or not all(c.isalnum() or c == '_' for c in username):
        print('❌ 用户名 3-30 位，仅限字母/数字/下划线')
        sys.exit(1)

    pwd1 = getpass.getpass('密码: ')
    pwd2 = getpass.getpass('再次输入密码: ')
    if pwd1 != pwd2:
        print('❌ 两次密码不一致')
        sys.exit(1)
    if len(pwd1) < 6:
        print('❌ 密码至少 6 位')
        sys.exit(1)

    salt_hex, hash_hex = hash_password(pwd1)
    conn = get_db()
    try:
        existing = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if existing:
            print(f'❌ 用户名 {username} 已存在')
            sys.exit(1)
        cur = conn.execute(
            '''INSERT INTO users (username, password_hash, password_salt, role, points_balance)
               VALUES (?,?,?,?,?)''',
            (username, hash_hex, salt_hex, 'admin', 0),
        )
        conn.commit()
        print(f'✅ 已创建管理员账号 {username} (id={cur.lastrowid})')
    finally:
        conn.close()


def cli_admin_list(args):
    init_db()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, username, role, status, points_balance, created_at "
            "FROM users WHERE role='admin' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print('（暂无管理员）')
        return
    print(f'{"id":<4} {"username":<20} {"status":<10} {"balance":<10} {"created_at"}')
    print('-' * 70)
    for r in rows:
        print(f'{r["id"]:<4} {r["username"]:<20} {r["status"]:<10} {r["points_balance"]:<10} {r["created_at"]}')


def cli_invite_create(args):
    """python3 resume-coach-server.py invite create [--points N] [--n M] [--note "xxx"] [--days D]"""
    init_db()
    points = DEFAULT_INVITE_POINTS
    count = 1
    note = ''
    days = DEFAULT_INVITE_EXPIRE_DAYS

    i = 0
    while i < len(args):
        a = args[i]
        if a == '--points' and i + 1 < len(args):
            points = int(args[i + 1]); i += 2
        elif a == '--n' and i + 1 < len(args):
            count = int(args[i + 1]); i += 2
        elif a == '--note' and i + 1 < len(args):
            note = args[i + 1]; i += 2
        elif a == '--days' and i + 1 < len(args):
            days = int(args[i + 1]); i += 2
        else:
            print(f'未知参数：{a}')
            sys.exit(1)

    conn = get_db()
    codes = []
    try:
        for _ in range(count):
            # 防碰撞重试
            for _attempt in range(10):
                code = generate_invitation_code()
                try:
                    conn.execute(
                        '''INSERT INTO invitation_codes (code, grant_points, expires_at, note)
                           VALUES (?,?,?,?)''',
                        (code, points, utc_future_iso(days), note or None),
                    )
                    codes.append(code)
                    break
                except sqlite3.IntegrityError:
                    continue
        conn.commit()
    finally:
        conn.close()
    print(f'✅ 已生成 {len(codes)} 张邀请码（每张 {points} 积分，{days} 天有效）')
    for c in codes:
        print(f'  {c}')


def cli_recharge_create(args):
    """python3 resume-coach-server.py recharge create [--points N] [--n M] [--bound-user U] [--days D] [--note "..."]"""
    init_db()
    points = 100
    count = 1
    bound = None
    days = 60
    note = ''
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--points' and i + 1 < len(args):
            points = int(args[i + 1]); i += 2
        elif a == '--n' and i + 1 < len(args):
            count = int(args[i + 1]); i += 2
        elif a == '--bound-user' and i + 1 < len(args):
            bound_arg = args[i + 1]
            # 支持 id 或 username
            try:
                bound = int(bound_arg)
            except ValueError:
                conn = get_db()
                try:
                    row = conn.execute('SELECT id FROM users WHERE username=?', (bound_arg,)).fetchone()
                finally:
                    conn.close()
                if not row:
                    print(f'❌ 找不到用户 {bound_arg}')
                    sys.exit(1)
                bound = row['id']
            i += 2
        elif a == '--days' and i + 1 < len(args):
            days = int(args[i + 1]); i += 2
        elif a == '--note' and i + 1 < len(args):
            note = args[i + 1]; i += 2
        else:
            print(f'未知参数：{a}')
            sys.exit(1)

    conn = get_db()
    codes = []
    try:
        for _ in range(count):
            for _attempt in range(10):
                code = generate_invitation_code()  # 复用同款生成器（8 位 A-Z+0-9）
                try:
                    conn.execute(
                        '''INSERT INTO recharge_codes
                           (code, grant_points, bound_user_id, expires_at, note)
                           VALUES (?,?,?,?,?)''',
                        (code, points, bound, utc_future_iso(days), note or None),
                    )
                    codes.append(code)
                    break
                except sqlite3.IntegrityError:
                    continue
        conn.commit()
    finally:
        conn.close()
    kind = '实名券' + (f'（绑定 user_id={bound}）' if bound else '') if bound else '通用券'
    print(f'✅ 已生成 {len(codes)} 张充值码（{kind}，每张 {points} 积分，{days} 天有效）')
    for c in codes:
        print(f'  {c}')


def cli_recharge_list(args):
    init_db()
    status = None
    if args and args[0] == '--status' and len(args) >= 2:
        status = args[1]
    conn = get_db()
    try:
        q = 'SELECT code, status, grant_points, bound_user_id, used_by_user_id, created_at, expires_at, note FROM recharge_codes'
        params = ()
        if status:
            q += ' WHERE status = ?'; params = (status,)
        q += ' ORDER BY created_at DESC LIMIT 200'
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()
    if not rows:
        print('（暂无充值码）')
        return
    print(f'{"code":<10} {"status":<8} {"pts":<6} {"bound":<8} {"used_by":<8} {"created_at":<20} {"note"}')
    print('-' * 80)
    for r in rows:
        b = str(r['bound_user_id']) if r['bound_user_id'] else '-'
        u = str(r['used_by_user_id']) if r['used_by_user_id'] else '-'
        n = (r['note'] or '')[:20]
        print(f'{r["code"]:<10} {r["status"]:<8} {r["grant_points"]:<6} {b:<8} {u:<8} {r["created_at"]:<20} {n}')


def handle_cli(argv):
    """argv 是 sys.argv[1:]"""
    if not argv:
        return False  # 不是 CLI 命令，走默认 server 启动
    cmd = argv[0]
    rest = argv[1:]

    if cmd == 'admin':
        if not rest:
            print('用法: admin create [username] | admin list')
            sys.exit(1)
        sub = rest[0]
        if sub == 'create':
            cli_admin_create(rest[1:])
        elif sub == 'list':
            cli_admin_list(rest[1:])
        else:
            print(f'未知子命令: admin {sub}')
            sys.exit(1)
        return True

    if cmd == 'invite':
        if not rest:
            print('用法: invite create [--points N] [--n M] [--days D] [--note "..."]')
            sys.exit(1)
        if rest[0] == 'create':
            cli_invite_create(rest[1:])
        else:
            print(f'未知子命令: invite {rest[0]}')
            sys.exit(1)
        return True

    if cmd == 'recharge':
        if not rest:
            print('用法: recharge create [--points N] [--n M] [--bound-user U] [--days D] [--note "..."]')
            print('      recharge list [--status unused|used|revoked|expired]')
            sys.exit(1)
        sub = rest[0]
        if sub == 'create':
            cli_recharge_create(rest[1:])
        elif sub == 'list':
            cli_recharge_list(rest[1:])
        else:
            print(f'未知子命令: recharge {sub}')
            sys.exit(1)
        return True

    return False


# =============================================================================
# 主入口
# =============================================================================
def main():
    # 优先处理 CLI 命令
    if handle_cli(sys.argv[1:]):
        return

    # 自检：claude CLI
    try:
        subprocess.run(['claude', '--version'], capture_output=True, text=True, timeout=5)
        claude_ok = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        claude_ok = False

    if not os.path.exists(HTML_FILE):
        print(f'❌ 找不到 {HTML_FILE}')
        sys.exit(1)
    if not os.path.exists(LANDING_FILE):
        print(f'⚠️  找不到 {LANDING_FILE}（landing page 将不可用，但 /app 仍可用）')

    # 端口冲突检测
    if port_in_use(PORT):
        print(f'❌ 端口 {PORT} 已被占用。先杀掉旧进程：')
        print(f'   lsof -ti:{PORT} | xargs kill -9')
        sys.exit(1)

    # 初始化数据库（建目录、建表、开 WAL）
    init_db()

    # 自检：有没有管理员（提示用）
    try:
        conn = get_db()
        admin_count = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()['n']
        conn.close()
    except Exception:
        admin_count = -1

    print('╭───────────────────────────────────────────────────╮')
    print('│  Resume Coach 后端 V2（并发 + 容错 + 用户体系）   │')
    print('├───────────────────────────────────────────────────┤')
    print(f'│  地址：http://localhost:{PORT}/                       │')
    print(f'│  claude CLI：{"✅ 已找到" if claude_ok else "❌ 未找到":<37} │')
    print(f'│  数据库：{DB_PATH:<43} │')
    if admin_count == 0:
        print('│  ⚠️  暂无管理员账号                                │')
        print('│     先在另一个终端跑：                              │')
        print('│     python3 resume-coach-server.py admin create   │')
    elif admin_count > 0:
        print(f'│  管理员账号数：{admin_count:<37}│')
    print('│  按 Ctrl+C 停止服务                                │')
    print('╰───────────────────────────────────────────────────╯\n')

    # 云端 ($PORT 注入) 时绑定 0.0.0.0，让容器外能访问；本地保持 localhost
    bind_host = '0.0.0.0' if os.environ.get('PORT') else 'localhost'
    try:
        server = ResumeServer((bind_host, PORT), Handler)
    except OSError as e:
        print(f'❌ 无法启动 server：{e}')
        sys.exit(1)

    threading.Thread(target=open_browser_after_delay, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n服务已停止。')
        server.shutdown()


if __name__ == '__main__':
    main()
