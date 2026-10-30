# -*- coding: utf-8 -*-
"""
FastAPI 入口：路由注册 + 生命周期（建表/构建知识库）+ 全局异常处理

页面路由（服务端渲染，SaaS 风格）：
    /login              统一登录页（公开）
    /                   员工聊天页（前端 JS 校验登录态）
    /admin              管理后台-仪表盘（仅管理员，Cookie 校验）
    /admin/knowledge    知识库管理（批2 填充）
    /admin/prompts      提示词模板（批3 填充）
    /admin/audit        对话审计（批4 填充）

启动方式（在 src 目录下执行）：
    uvicorn app.main:app --host 127.0.0.1 --port 8765
"""
import io
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, security
from app.chat import build_chat_response
from app.rag import CONFIG, KB
from app.schemas import (ApiError, ChatRequest, CODE_INTERNAL, CODE_PARAM,
                         LoginRequest, fail, ok, public_body)

# 路径：基于文件定位，与启动目录无关
SRC_DIR = Path(__file__).resolve().parent.parent        # src/
KB_DIR = SRC_DIR / "data" / "knowledge"                 # 制度 md 目录
STATIC_DIR = SRC_DIR / "static"                         # 前端静态目录
TEMPLATE_DIR = SRC_DIR / "templates"                    # 服务端渲染模板

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时建库表 + 构建知识库索引"""
    db.init_db()
    n = KB.build(KB_DIR)
    print(f"[启动] 知识库就绪：{n} 个切片 | LLM 供应商：{CONFIG['llm_provider']}")
    yield


app = FastAPI(title="跨区域入职材料一次告知 · 制度智能问答", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# =====================================================================
# 认证接口（批1）
# =====================================================================

@app.post("/api/auth/login")
async def auth_login(req: LoginRequest):
    """登录：校验口令 → 签发 JWT。

    双通道下发：JSON 里给 token（前端 fetch 用 Authorization 头），
    同时写 HttpOnly Cookie（服务端渲染的后台页面用）。
    """
    user = db.get_user_by_username(req.username)
    if user is None or not security.verify_password(req.password, user["password_hash"]):
        raise ApiError(1006, "用户名或密码错误", 401)

    token = security.jwt_encode({
        "sub": user["id"],
        "role": user["role"],
        "name": user["name"],
        "username": user["username"],
    })
    resp = JSONResponse(content=ok({
        "token": token,
        "role": user["role"],
        "name": user["name"],
        "redirect": "/admin" if user["role"] == "admin" else "/",
    }))
    # HttpOnly：JS 不可读，防 XSS 窃取；SameSite=Lax 防 CSRF 基础面
    resp.set_cookie("access_token", token, max_age=security.TOKEN_TTL_SECONDS,
                    httponly=True, samesite="lax")
    return resp


@app.get("/api/auth/me")
async def auth_me(user: dict = Depends(security.get_current_user)):
    """当前登录用户信息（前端刷新页面时校验登录态）"""
    return ok(user)


@app.post("/api/auth/logout")
async def auth_logout():
    """退出登录：清除 Cookie（JWT 无状态，令牌自然过期即可）"""
    resp = JSONResponse(content=ok(message="已退出"))
    resp.delete_cookie("access_token")
    return resp


# =====================================================================
# 页面路由
# =====================================================================

@app.get("/login")
async def login_page(request: Request):
    """统一登录页；已登录的管理员直接进后台"""
    if security.admin_from_request(request):
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/")
async def index():
    """员工聊天页（登录态由前端 JS 调 /api/auth/me 校验）"""
    return FileResponse(STATIC_DIR / "index.html")


def _admin_context(request: Request, active: str, title: str) -> dict | None:
    """后台页面公共上下文：Cookie 校验管理员，失败返回 None（路由重定向）"""
    user = security.admin_from_request(request)
    if user is None:
        return None
    return {"request": request, "user": user, "active": active, "title": title}


@app.get("/admin")
async def admin_dashboard(request: Request):
    """仪表盘：核心统计卡片（批1先上会话/消息/知识库三项）"""
    ctx = _admin_context(request, "dashboard", "仪表盘")
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    ctx["stats"] = {
        "sessions": db.count_sessions(),
        "messages": db.count_messages(),
        "kb_docs": len(db.list_docs()),
        "kb_chunks": len(KB.chunks),
        "violations": db.count_violations(),       # 批4：真实违规统计
    }
    return templates.TemplateResponse(request, "admin_dashboard.html", ctx)


@app.get("/admin/knowledge")
async def admin_knowledge(request: Request):
    """知识库管理页（批2）：上传 md/pdf / 文档列表 / 删除"""
    ctx = _admin_context(request, "knowledge", "制度知识库")
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "admin_knowledge.html", ctx)


@app.get("/admin/prompts")
async def admin_prompts(request: Request):
    """提示词模板页（批3）：编辑当前模板 / 版本历史 / 回滚"""
    ctx = _admin_context(request, "prompts", "提示词模板")
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "admin_prompts.html", ctx)


@app.get("/admin/audit")
async def admin_audit(request: Request):
    """对话审计页（批4）：按员工/时间筛选，标记疑似违规问答"""
    ctx = _admin_context(request, "audit", "对话审计")
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    ctx["employees"] = db.list_employees()
    return templates.TemplateResponse(request, "admin_audit.html", ctx)


# =====================================================================
# 员工问答接口（批1 起需登录）
# =====================================================================

@app.post("/chat")
async def chat(req: ChatRequest, user: dict = Depends(security.get_current_user)):
    """员工问答入口（SSE 流式）。登录即可问答（员工/管理员都允许）。"""
    _ = user   # 依赖已校验登录态；角色不限制
    if not KB.ready:
        raise ApiError(1003, "知识库未初始化或为空，请联系管理员检查制度文档", 503)
    if req.session_id and db.get_session(req.session_id) is None:
        raise ApiError(1002, "会话不存在，请开启新会话", 404)
    return build_chat_response(req)


@app.get("/sessions/{session_id}/history")
async def history(session_id: str, user: dict = Depends(security.get_current_user)):
    """会话历史回放（需登录）"""
    if db.get_session(session_id) is None:
        raise ApiError(1002, "会话不存在", 404)
    messages = [
        {
            "id": m["id"],
            "role": m["role"],
            "content": m["content"],
            "citations": (m["meta"] or {}).get("citations", []),
            "created_at": m["created_at"],
        }
        for m in db.get_all_messages(session_id)
    ]
    return ok({"session_id": session_id, "messages": messages})


@app.get("/health")
async def health():
    """健康检查：无需登录（存活探测）"""
    return ok({
        "status": "ok",
        "knowledge_ready": KB.ready,
        "chunks": len(KB.chunks),
        "llm_provider": CONFIG["llm_provider"],
    })


# =====================================================================
# 管理后台 API（批2 知识库 / 批3 提示词 / 批4 审计）—— 全部仅管理员
# =====================================================================

def _admin(user: dict) -> dict:
    """接口级管理员校验的统一入口：登录 + 角色 两道闸"""
    return security.require_admin(user)


def _extract_pdf(data: bytes) -> str:
    """PDF → 纯文本（pypdf 逐页提取，空页跳过）"""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)


def _rebuild_kb_and_sync() -> int:
    """重建知识库索引，并把各文档切片数回填 policy_docs（批2 上传/删除后调用）"""
    n = KB.build(KB_DIR)
    db.sync_doc_chunk_counts(KB.chunk_count_by_source())
    return n


@app.post("/api/admin/kb/upload")
async def kb_upload(file: UploadFile = File(...), user: dict = Depends(security.get_current_user)):
    """上传制度文档：.md 直接入库；.pdf 提取文本 → 切片 → 向量化 → 重建索引"""
    _admin(user)

    # ---- 1. 校验类型与大小 ----
    original = file.filename or "未命名"
    ext = original.lower().rsplit(".", 1)[-1] if "." in original else ""
    if ext not in ("md", "markdown", "pdf"):
        raise ApiError(1001, "仅支持上传 .md 或 .pdf 格式的制度文档", 400)
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise ApiError(1001, "文件大小不能超过 10MB", 400)

    # ---- 2. 提取文本（md 直读；pdf 走 pypdf）----
    if ext == "pdf":
        text = _extract_pdf(raw)
    else:
        text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise ApiError(1001, "未能从文件中提取到有效文本内容", 400)

    # ---- 3. 落盘：{doc_id}__{安全化原名}.md（doc_id 前缀保证唯一且可删除定位）----
    doc_id = uuid.uuid4().hex
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", original)     # 去掉文件系统非法字符
    filename = f"{doc_id}__{safe_name}.md"
    (KB_DIR / filename).write_text(text, encoding="utf-8")

    # ---- 4. 登记文档 → 重建索引 → 回填切片数 ----
    db.insert_doc(doc_id, filename, original, "md" if ext != "pdf" else "pdf", user["user_id"])
    total = _rebuild_kb_and_sync()
    return ok({"doc_id": doc_id, "chunks_total": total},
              message=f"文档「{original}」已入库并生效")


@app.get("/api/admin/kb/list")
async def kb_list(user: dict = Depends(security.get_current_user)):
    """制度文档列表（含各文档切片数）"""
    _admin(user)
    return ok(db.list_docs())


@app.delete("/api/admin/kb/{doc_id}")
async def kb_delete(doc_id: str, user: dict = Depends(security.get_current_user)):
    """删除制度文档：删文件 → 软删记录 → 重建索引（立即不再参与检索）"""
    _admin(user)
    doc = db.get_doc(doc_id)
    if doc is None:
        raise ApiError(1002, "文档不存在或已删除", 404)
    path = KB_DIR / doc["filename"]
    if path.exists():
        path.unlink()                                        # 删除切片源文件
    db.mark_doc_deleted(doc_id)
    total = _rebuild_kb_and_sync()
    return ok({"chunks_total": total}, message=f"文档「{doc['original_name']}」已删除")


@app.get("/api/admin/prompt")
async def prompt_get(user: dict = Depends(security.get_current_user)):
    """当前生效的系统提示词与版本号"""
    _admin(user)
    versions = db.list_prompt_versions()
    active = next((v for v in versions if v["is_active"]), None)
    return ok({"content": active["content"] if active else None,
               "version": active["version"] if active else None})


@app.put("/api/admin/prompt")
async def prompt_put(request: Request, user: dict = Depends(security.get_current_user)):
    """保存新版本提示词并激活：问答编排实时读库 → 下一次问答即生效"""
    admin = _admin(user)
    body = await request.json()
    content = (body.get("content") or "").strip()
    if not content:
        raise ApiError(1001, "提示词内容不能为空", 400)
    if len(content) > 4000:
        raise ApiError(1001, "提示词长度不能超过 4000 字", 400)
    result = db.add_prompt_version(content, admin["user_id"])
    return ok(result, message=f"已保存并生效（版本 v{result['version']}）")


@app.get("/api/admin/prompt/versions")
async def prompt_versions(user: dict = Depends(security.get_current_user)):
    """提示词版本历史（倒序）"""
    _admin(user)
    return ok(db.list_prompt_versions())


@app.post("/api/admin/prompt/versions/{pid}/activate")
async def prompt_activate(pid: str, user: dict = Depends(security.get_current_user)):
    """回滚：把指定历史版本设为当前生效"""
    _admin(user)
    if not db.activate_prompt_version(pid):
        raise ApiError(1002, "版本不存在", 404)
    return ok(message="已激活该版本，立即生效")


@app.get("/api/admin/audit")
async def audit_list(request: Request, user: dict = Depends(security.get_current_user)):
    """对话审计查询：员工工号 / 起止日期 / 仅看疑似违规"""
    _admin(user)
    q = request.query_params
    rows = db.audit_query(
        employee_no=q.get("employee_no") or None,
        start=q.get("start") or None,
        end=q.get("end") or None,
        violation_only=q.get("violation_only") in ("1", "true"),
    )
    return ok(rows)


@app.get("/api/admin/audit/stats")
async def audit_stats(user: dict = Depends(security.get_current_user)):
    """审计统计概览（仪表盘卡片）"""
    _admin(user)
    return ok({"violations": db.count_violations(),
               "messages": db.count_messages()})


# =====================================================================
# 全局异常处理：所有错误统一为 {code, message, data} 中文结构
# =====================================================================

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    """业务异常 → 对应错误码与中文信息"""
    return JSONResponse(status_code=exc.http_status,
                        content=public_body(fail(exc.code, exc.message)))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """参数校验失败 → 1001，拼接 pydantic 的字段级错误信息"""
    detail = "；".join(
        f"{'.'.join(str(loc) for loc in err['loc'][1:])}：{err['msg']}"
        for err in exc.errors()
    )
    return JSONResponse(status_code=400,
                        content=public_body(fail(CODE_PARAM, f"参数校验失败：{detail}")))


@app.exception_handler(Exception)
async def internal_error_handler(request: Request, exc: Exception):
    """兜底异常 → 5000（不向前端泄露堆栈细节）"""
    return JSONResponse(status_code=500,
                        content=public_body(fail(CODE_INTERNAL, "系统内部错误，请稍后重试")))
