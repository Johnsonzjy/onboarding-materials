# -*- coding: utf-8 -*-
"""
数据访问层：SQLite（沿用阶段 1 表结构中的 users / sessions / messages 三张表）
- 本阶段内置一个 demo 员工账号，/chat 暂不做鉴权（SSO 留给阶段 2 后续）
- 使用 sqlite3 + 线程锁：原型阶段读写极轻量，够用且零依赖
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

# 数据库文件位置：src/chat.db（基于文件路径定位，与启动目录无关）
DB_PATH = Path(__file__).resolve().parent.parent / "chat.db"

# 内置 demo 员工（阶段 2 后续替换为 SSO 登录用户）
DEMO_USER_ID = "demo-employee"

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()   # sqlite3 连接非线程安全，用锁串行化写操作


def _now() -> str:
    """统一时间格式（本地时间）"""
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    """生成 32 位无连字符的 UUID 作为主键"""
    return uuid.uuid4().hex


def init_db() -> None:
    """建表 + 种子数据（幂等：IF NOT EXISTS，重复启动不报错）"""
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    cur = _conn.cursor()

    # ---- 用户表（沿用阶段 1 设计，字段有裁剪：去掉 SSO 相关留待后续）----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          TEXT PRIMARY KEY,
            employee_no TEXT UNIQUE,
            name        TEXT,
            role        TEXT,              -- employee / admin
            org_id      TEXT,              -- 租户/法人
            region_code TEXT,              -- 归属区域（影响政策召回）
            status      TEXT,              -- active / disabled
            created_at  TEXT
        )
    """)

    # ---- 会话表 ----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id             TEXT PRIMARY KEY,
            user_id        TEXT,
            title          TEXT,           -- 首条消息自动生成（前 20 字）
            channel        TEXT,           -- web / app / wecom
            status         TEXT,           -- active / closed
            created_at     TEXT,
            last_active_at TEXT
        )
    """)

    # ---- 消息表 ----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id         TEXT PRIMARY KEY,
            session_id TEXT,
            role       TEXT,               -- user / assistant / system / tool
            content    TEXT,
            meta       TEXT,               -- JSON 字符串：存 citations 等附加信息
            sequence   INTEGER,            -- 会话内顺序号（从 1 递增）
            created_at TEXT
        )
    """)

    # ---- 种子：内置 demo 员工（存在则跳过）----
    cur.execute("SELECT 1 FROM users WHERE id = ?", (DEMO_USER_ID,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?,?)",
            (DEMO_USER_ID, "DEMO001", "演示员工", "employee",
             "demo-org", "BJ", "active", _now()),
        )
    _conn.commit()


def create_session(user_id: str, title: str, channel: str = "web") -> str:
    """新建会话，返回 session_id；title 取首问前 20 字"""
    sid = _new_id()
    with _lock:
        _conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",   # 7 列：id/user/title/channel/status/created/active
            (sid, user_id, title[:20], channel, "active", _now(), _now()),
        )
        _conn.commit()
    return sid


def get_session(session_id: str) -> dict | None:
    """按 id 查会话，不存在返回 None（路由层据此抛 1002）"""
    row = _conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def touch_session(session_id: str) -> None:
    """更新会话活跃时间"""
    with _lock:
        _conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE id = ?",
            (_now(), session_id),
        )
        _conn.commit()


def add_message(session_id: str, role: str, content: str, meta: dict | None = None) -> str:
    """写一条消息；sequence = 会话内现有条数 + 1；meta 序列化为 JSON 存储"""
    mid = _new_id()
    with _lock:
        cnt = _conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()["c"]
        _conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
            (mid, session_id, role, content,
             json.dumps(meta, ensure_ascii=False) if meta else None,
             cnt + 1, _now()),
        )
        _conn.commit()
    return mid


def get_recent_messages(session_id: str, limit: int = 13) -> list[dict]:
    """取某会话最近 limit 条消息（返回按时间正序）。

    调用约定：/chat 编排取 13 条后去掉最后一条（当前刚插入的用户问题），
    剩余 ≤12 条（= 6 轮）作为上下文历史。
    """
    rows = _conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY sequence DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    rows = [dict(r) for r in reversed(rows)]        # 反转成时间正序
    for r in rows:                                   # meta 由 JSON 字符串还原为 dict
        r["meta"] = json.loads(r["meta"]) if r["meta"] else None
    return rows


def get_all_messages(session_id: str) -> list[dict]:
    """取会话全部消息（历史回放接口用），按时间正序"""
    rows = _conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY sequence",
        (session_id,),
    ).fetchall()
    rows = [dict(r) for r in rows]
    for r in rows:
        r["meta"] = json.loads(r["meta"]) if r["meta"] else None
    return rows
