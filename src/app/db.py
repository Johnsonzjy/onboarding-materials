# -*- coding: utf-8 -*-
"""
数据访问层：SQLite
- users / sessions / messages（沿用阶段 1 表结构）
- policy_docs（批2：制度文档元数据，文件本体存 data/knowledge/{doc_id}__原名.md）
- prompt_templates（批3：提示词模板多版本，问答实时读取生效版本）
- 审计查询（批4：消息级联员工/会话 + 违规标记筛选）
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from app.security import hash_password

# 数据库文件位置：src/chat.db（基于文件路径定位，与启动目录无关）
DB_PATH = Path(__file__).resolve().parent.parent / "chat.db"

# 内置账号（批1：员工用 JWT 登录聊天，管理员进后台）
DEMO_USER_ID = "demo-employee"     # 员工账号 id（沿用，历史会话归属不受影响）
ADMIN_USER_ID = "admin-001"        # 管理员账号 id
SEED_ACCOUNTS = [
    # (id, 用户名, 姓名, 角色, 初始密码)
    (DEMO_USER_ID, "demo001", "演示员工", "employee", "123456"),
    (ADMIN_USER_ID, "admin", "系统管理员", "admin", "admin123"),
]

# 批3：默认系统提示词（需求方给定模板；存库不写死在问答逻辑里，可随时改）
DEFAULT_PROMPT = (
    "你是公司跨区域入职材料一次告知 · 业务挖掘助手，只根据提供的制度条文回答，"
    "不确定时明确说'制度中未找到'，回答末尾附引用来源，不得编造报销标准。"
)

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

    # ---- 用户表（批1 扩展：username / password_hash）----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            employee_no   TEXT UNIQUE,
            name          TEXT,
            role          TEXT,              -- employee / admin
            org_id        TEXT,              -- 租户/法人
            region_code   TEXT,              -- 归属区域（影响政策召回）
            status        TEXT,              -- active / disabled
            created_at    TEXT,
            username      TEXT UNIQUE,       -- 登录名（批1新增）
            password_hash TEXT              -- pbkdf2 哈希（批1新增）
        )
    """)
    # ---- 旧库迁移：已存在的 users 表补齐批1新增的两列（幂等）----
    cols = {r["name"] for r in cur.execute("PRAGMA table_info(users)")}
    if "username" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN username TEXT UNIQUE")
    if "password_hash" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")

    # ---- 旧库迁移：sessions 补 tool_state 列（阶段3 工具状态机：JSON 存
    #      {validated, draft_id, user_confirmed}，L2 硬约束的数据来源）----
    cols = {r["name"] for r in cur.execute("PRAGMA table_info(sessions)")}
    if "tool_state" not in cols:
        cur.execute("ALTER TABLE sessions ADD COLUMN tool_state TEXT")

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
            meta       TEXT,               -- JSON：citations / violation 等附加信息
            sequence   INTEGER,            -- 会话内顺序号（从 1 递增）
            created_at TEXT
        )
    """)

    # ---- 制度文档表（批2：知识库管理）----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS policy_docs (
            id            TEXT PRIMARY KEY,   -- doc_id，同时是存储文件名前缀
            filename      TEXT,               -- 存储文件名：{doc_id}__{原名}.md
            original_name TEXT,               -- 上传时的原始文件名
            file_type     TEXT,               -- md / pdf（pdf 已提取为文本）
            chunk_count   INTEGER,            -- 该文档贡献的切片数（重建索引后回填）
            status        TEXT,               -- active / deleted
            uploaded_by   TEXT,               -- 上传管理员 user_id
            created_at    TEXT
        )
    """)

    # ---- 提示词模板表（批3：多版本，is_active=1 为当前生效）----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id         TEXT PRIMARY KEY,
            content    TEXT,               -- 模板正文
            version    INTEGER,            -- 版本号（递增）
            is_active  INTEGER,            -- 1=当前生效
            updated_by TEXT,               -- 修改人 user_id
            updated_at TEXT
        )
    """)

    # ---- 报销单草稿表（阶段3：智能体 create_draft / submit_reimbursement 落数）----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reimbursement_drafts (
            id          TEXT PRIMARY KEY,   -- draft_id
            session_id  TEXT,               -- 由哪次会话产生
            user_id     TEXT,               -- 归属员工
            type        TEXT,               -- travel / entertainment / material / other
            status      TEXT,               -- draft（草稿）/ submitted（已提交）
            amount      REAL,               -- 金额（元）
            currency    TEXT,               -- 币种，默认 CNY
            reason      TEXT,               -- 报销事由
            items       TEXT,               -- 明细行 JSON
            summary     TEXT,               -- Agent 生成的单据摘要
            created_at  TEXT,
            submitted_at TEXT               -- 提交时间（草稿态为 NULL）
        )
    """)

    # ---- 种子：内置员工/管理员账号（存在则跳过；密码只存 pbkdf2 哈希）----
    for uid, username, name, role, password in SEED_ACCOUNTS:
        cur.execute("SELECT 1 FROM users WHERE id = ?", (uid,))
        if cur.fetchone() is None:
            cur.execute(
                # 显式列名插入（users 表后续加列时不受影响）
                "INSERT INTO users (id, employee_no, name, role, org_id, region_code, "
                "status, created_at, username, password_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uid, username.upper(), name, role, "demo-org", "BJ",
                 "active", _now(), username, hash_password(password)),
            )

    # ---- 种子：默认系统提示词（需求方给定模板，仅首次初始化写入）----
    cur.execute("SELECT 1 FROM prompt_templates LIMIT 1")
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO prompt_templates (id, content, version, is_active, "
            "updated_by, updated_at) VALUES (?,?,?,?,?,?)",
            (_new_id(), DEFAULT_PROMPT, 1, 1, "system", _now()),
        )
    _conn.commit()


# =====================================================================
# 会话与消息（阶段2 已有）
# =====================================================================

def create_session(user_id: str, title: str, channel: str = "web") -> str:
    """新建会话，返回 session_id；title 取首问前 20 字"""
    sid = _new_id()
    with _lock:
        _conn.execute(
            # 显式列名插入：新增列（如 tool_state）有默认值时不受影响
            "INSERT INTO sessions (id, user_id, title, channel, status, "
            "created_at, last_active_at) VALUES (?,?,?,?,?,?,?)",
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


# =====================================================================
# 用户（批1）
# =====================================================================

def get_user_by_username(username: str) -> dict | None:
    """按登录名查用户（登录接口用）"""
    row = _conn.execute(
        "SELECT * FROM users WHERE username = ? AND status = 'active'",
        (username,),
    ).fetchone()
    return dict(row) if row else None


# =====================================================================
# 制度文档（批2）
# =====================================================================

def insert_doc(doc_id: str, filename: str, original_name: str,
               file_type: str, uploaded_by: str) -> None:
    """登记一个新上传的制度文档（切片数待索引重建后回填）"""
    with _lock:
        _conn.execute(
            "INSERT INTO policy_docs VALUES (?,?,?,?,?,?,?,?)",   # 8 列
            (doc_id, filename, original_name, file_type, 0, "active",
             uploaded_by, _now()),
        )
        _conn.commit()


def sync_doc_chunk_counts(count_by_filename: dict[str, int]) -> None:
    """索引重建后，把每个文档的切片数回填（以存储文件名为键）"""
    with _lock:
        for filename, cnt in count_by_filename.items():
            _conn.execute(
                "UPDATE policy_docs SET chunk_count = ? WHERE filename = ?",
                (cnt, filename),
            )
        _conn.commit()


def list_docs() -> list[dict]:
    """文档列表（知识库管理页），按上传时间倒序"""
    rows = _conn.execute(
        "SELECT * FROM policy_docs WHERE status = 'active' ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_doc(doc_id: str) -> dict | None:
    """按 id 查文档"""
    row = _conn.execute("SELECT * FROM policy_docs WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def mark_doc_deleted(doc_id: str) -> None:
    """软删除文档记录（文件由路由层负责删除）"""
    with _lock:
        _conn.execute(
            "UPDATE policy_docs SET status = 'deleted' WHERE id = ?", (doc_id,)
        )
        _conn.commit()


# =====================================================================
# 提示词模板（批3）
# =====================================================================

def get_active_prompt() -> str | None:
    """当前生效的系统提示词（问答编排每次实时读取 → 保存即生效）"""
    row = _conn.execute(
        "SELECT content FROM prompt_templates WHERE is_active = 1 "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return row["content"] if row else None


def add_prompt_version(content: str, updated_by: str) -> dict:
    """保存新版本并激活：旧版本全部置为 inactive，新版本 version+1"""
    with _lock:
        max_v = _conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM prompt_templates"
        ).fetchone()["v"]
        pid = _new_id()
        _conn.execute("UPDATE prompt_templates SET is_active = 0 WHERE is_active = 1")
        _conn.execute(
            "INSERT INTO prompt_templates (id, content, version, is_active, "
            "updated_by, updated_at) VALUES (?,?,?,?,?,?)",
            (pid, content, max_v + 1, 1, updated_by, _now()),
        )
        _conn.commit()
    return {"id": pid, "version": max_v + 1}


def list_prompt_versions() -> list[dict]:
    """版本历史（倒序），含是否当前生效"""
    rows = _conn.execute(
        "SELECT * FROM prompt_templates ORDER BY version DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def activate_prompt_version(pid: str) -> bool:
    """回滚：激活指定历史版本"""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM prompt_templates WHERE id = ?", (pid,)
        ).fetchone()
        if row is None:
            return False
        _conn.execute("UPDATE prompt_templates SET is_active = 0 WHERE is_active = 1")
        _conn.execute(
            "UPDATE prompt_templates SET is_active = 1 WHERE id = ?", (pid,)
        )
        _conn.commit()
    return True


# =====================================================================
# 统计与审计（批4）
# =====================================================================

def count_sessions() -> int:
    """会话总数（仪表盘统计）"""
    return _conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]


def count_messages() -> int:
    """消息总数（仪表盘统计）"""
    return _conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]


def count_violations() -> int:
    """疑似违规消息数：meta JSON 中含 violation 键即计入"""
    return _conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE meta LIKE '%\"violation\"%'"
    ).fetchone()["c"]


def audit_query(employee_no: str | None = None, start: str | None = None,
                end: str | None = None, violation_only: bool = False,
                limit: int = 200) -> list[dict]:
    """审计查询：消息级联会话与员工，支持多维筛选。

    - employee_no：按员工工号筛选
    - start / end：按消息时间筛选（ISO 字符串比较，支持 '2026-10-30' 或完整时间戳）
    - violation_only：仅看疑似违规（meta.violation 非空）
    返回按时间倒序，上限 limit 条（原型阶段够用）。
    """
    sql = """
        SELECT m.id, m.session_id, m.role, m.content, m.meta, m.created_at,
               u.name AS employee_name, u.employee_no
        FROM messages m
        JOIN sessions s ON m.session_id = s.id
        JOIN users u ON s.user_id = u.id
        WHERE 1=1
    """
    params: list = []
    if employee_no:
        sql += " AND u.employee_no = ?"
        params.append(employee_no.upper())
    if start:
        sql += " AND m.created_at >= ?"
        params.append(start)
    if end:
        sql += " AND m.created_at <= ?"        # 日期口径：'2026-10-30' 需补全为当天末尾
        params.append(f"{end} 23:59:59" if len(end) == 10 else end)

    rows = _conn.execute(sql + " ORDER BY m.created_at DESC LIMIT ?", 
                         (*params, limit)).fetchall()

    out = []
    for r in rows:
        meta = json.loads(r["meta"]) if r["meta"] else {}
        violation = meta.get("violation")      # 违规标记（可能为 None）
        if violation_only and not violation:
            continue
        out.append({
            "id": r["id"],
            "session_id": r["session_id"],
            "role": r["role"],
            "employee_name": r["employee_name"],
            "employee_no": r["employee_no"],
            "content": r["content"],
            "violation": violation,
            "created_at": r["created_at"],
        })
    return out


def list_employees() -> list[dict]:
    """员工下拉/筛选候选（审计页用）"""
    rows = _conn.execute(
        "SELECT DISTINCT u.employee_no, u.name FROM users u "
        "JOIN sessions s ON s.user_id = u.id"
    ).fetchall()
    return [dict(r) for r in rows]


# =====================================================================
# 工具状态机与报销单草稿（阶段3：智能体）
# =====================================================================

def get_tool_state(session_id: str) -> dict:
    """读取会话工具状态（L2 硬约束依据）：{validated, draft_id, user_confirmed}"""
    row = _conn.execute(
        "SELECT tool_state FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None or not row["tool_state"]:
        return {}
    try:
        return json.loads(row["tool_state"])
    except json.JSONDecodeError:
        return {}


def set_tool_state(session_id: str, state: dict) -> None:
    """写入会话工具状态（整包覆盖，调用方负责先 get 再改）"""
    with _lock:
        _conn.execute(
            "UPDATE sessions SET tool_state = ? WHERE id = ?",
            (json.dumps(state, ensure_ascii=False), session_id),
        )
        _conn.commit()


def insert_draft(session_id: str, user_id: str, dtype: str, amount: float,
                 reason: str, items: list, summary: str) -> dict:
    """新建报销单草稿（create_draft 工具落库），返回含 id 的完整记录"""
    did = _new_id()
    with _lock:
        _conn.execute(
            "INSERT INTO reimbursement_drafts (id, session_id, user_id, type, status, "
            "amount, currency, reason, items, summary, created_at, submitted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, session_id, user_id, dtype, "draft", amount, "CNY",
             reason, json.dumps(items, ensure_ascii=False), summary,
             _now(), None),
        )
        _conn.commit()
    return {"id": did, "amount": amount, "summary": summary}


def get_draft(draft_id: str) -> dict | None:
    """按 id 查报销单"""
    row = _conn.execute(
        "SELECT * FROM reimbursement_drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    return dict(row) if row else None


def set_draft_submitted(draft_id: str) -> None:
    """草稿 → 已提交（submit_reimbursement 工具落库）"""
    with _lock:
        _conn.execute(
            "UPDATE reimbursement_drafts SET status = 'submitted', submitted_at = ? "
            "WHERE id = ? AND status = 'draft'",
            (_now(), draft_id),
        )
        _conn.commit()


def list_drafts_by_user(user_id: str, limit: int = 20) -> list[dict]:
    """某员工的报销单记录（list_my_records 工具用，时间倒序）"""
    rows = _conn.execute(
        "SELECT id, type, status, amount, currency, reason, summary, "
        "created_at, submitted_at FROM reimbursement_drafts "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
