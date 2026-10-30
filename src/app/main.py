# -*- coding: utf-8 -*-
"""
FastAPI 入口：路由注册 + 生命周期（建表/构建知识库）+ 全局异常处理

启动方式（在 src 目录下执行）：
    uvicorn app.main:app --host 127.0.0.1 --port 8765
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.chat import build_chat_response
from app.rag import CONFIG, KB
from app.schemas import (ApiError, ChatRequest, CODE_INTERNAL, CODE_PARAM,
                         CODE_SESSION_NOT_FOUND, fail, ok, public_body)

# 路径：基于文件定位，与启动目录无关
SRC_DIR = Path(__file__).resolve().parent.parent        # src/
KB_DIR = SRC_DIR / "data" / "knowledge"                 # 制度 md 目录
STATIC_DIR = SRC_DIR / "static"                         # 前端静态目录


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时建库表 + 构建知识库索引；关闭时无额外清理"""
    db.init_db()
    n = KB.build(KB_DIR)
    print(f"[启动] 知识库就绪：{n} 个切片 | LLM 供应商：{CONFIG['llm_provider']}")
    yield


app = FastAPI(title="跨区域入职材料一次告知 · 制度智能问答", lifespan=lifespan)

# 挂载前端静态资源（index.html 中的 css/js 内联，这里主要占位扩展用）
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# =====================================================================
# 路由
# =====================================================================

@app.get("/")
async def index():
    """聊天页入口"""
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/chat")
async def chat(req: ChatRequest):
    """员工问答入口（SSE 流式）。

    校验顺序：知识库就绪 → 会话存在。参数校验由 Pydantic + 全局异常处理器完成。
    流开始后的错误走 event:error；流开始前的错误直接返回 JSON 错误结构。
    """
    if not KB.ready:
        raise ApiError(1003, "知识库未初始化或为空，请联系管理员检查制度文档", 503)
    if req.session_id and db.get_session(req.session_id) is None:
        raise ApiError(1002, "会话不存在，请开启新会话", 404)
    return build_chat_response(req)


@app.get("/sessions/{session_id}/history")
async def history(session_id: str):
    """会话历史回放（页面刷新后恢复消息列表，含引用）"""
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
    """健康检查：服务存活 + 知识库状态 + 当前 LLM 供应商"""
    return ok({
        "status": "ok",
        "knowledge_ready": KB.ready,
        "chunks": len(KB.chunks),
        "llm_provider": CONFIG["llm_provider"],
    })


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
