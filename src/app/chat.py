# -*- coding: utf-8 -*-
"""
/chat 接口的 SSE 编排层

事件时序（设计决策 1/2 已确认）：
  meta      → session_id（新建会话时告知前端）
  status    → "正在检索制度…"
  citations → 引用片段列表（正文输出前单独推送）
  status    → "正在生成回答…"
  delta     → 正文增量（逐段，前端打字机）
  done      → 结束信号（session_id + message_id）
  error     → 流内错误（code + 中文 message；流已开始无法改 HTTP 状态码）

会话 ID 只在 build_chat_response 中解析一次并传给生成器，保证前后一致。
"""
import json
from typing import AsyncGenerator

from fastapi.responses import StreamingResponse

from app import db
from app.audit import scan_violation
from app.rag import KB, stream_llm
from app.schemas import CODE_INTERNAL, CODE_LLM, ChatRequest


class LLMError(Exception):
    """LLM 专属异常标记：供编排层区分 3001 与 5000 错误码"""


def _sse(event: str, data) -> str:
    """格式化一条 SSE 帧（ensure_ascii=False 保证中文原样输出）"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _citations_of(hits: list[dict]) -> list[dict]:
    """把检索结果裁剪为引用结构：rank + 来源文件名 + 片段前 200 字"""
    return [{"rank": h["rank"], "source": h["source"], "snippet": h["text"][:200]}
            for h in hits]


async def chat_generator(session_id: str, req: ChatRequest) -> AsyncGenerator[str, None]:
    """SSE 主流程：告知会话 → 检索 → 6轮历史 + RAG 提示词 → 流式生成 → 落库"""
    # ---- 1. 先把会话 ID 告知前端（新建会话时前端据此保存） ----
    yield _sse("meta", {"session_id": session_id})

    # ---- 2. 混合检索 Top3：先推"检索中"状态，再推引用列表 ----
    yield _sse("status", {"stage": "retrieving", "message": "正在检索制度…"})
    try:
        hits = KB.hybrid_search(req.question)
        citations = _citations_of(hits)
        yield _sse("citations", citations)

        # ---- 3. 组装 6 轮历史：取最近 13 条，去掉最后一条（本轮刚插入的问题），
        #         剩余 ≤12 条（= 6 轮 user+assistant）作为上下文 ----
        yield _sse("status", {"stage": "generating", "message": "正在生成回答…"})
        rows = db.get_recent_messages(session_id, limit=13)
        history = rows[:-1] if rows else []

        # ---- 批3：系统提示词实时读库（管理员改模板 → 下一次问答即生效）----
        system_prompt = db.get_active_prompt()

        # ---- 4. 流式生成：逐段推 delta，同时累计全文 ----
        full_answer: list[str] = []
        async for piece in _generate_safe(history, req.question, hits, system_prompt):
            full_answer.append(piece)
            yield _sse("delta", {"content": piece})

        # ---- 5. 回答落库（meta 存引用）→ 更新会话活跃时间 → 推 done ----
        answer_text = "".join(full_answer)
        mid = db.add_message(session_id, "assistant", answer_text,
                             meta={"citations": citations})
        db.touch_session(session_id)
        yield _sse("done", {"session_id": session_id, "message_id": mid})

    except LLMError:
        # 模型调用失败（供应商不可达/超时/返回异常）→ 3001
        yield _sse("error", {"code": CODE_LLM,
                             "message": "大模型调用失败，请稍后重试"})
    except Exception:   # noqa: BLE001 —— 流内其余异常 → 5000
        yield _sse("error", {"code": CODE_INTERNAL,
                             "message": "系统内部错误，请稍后重试"})


async def _generate_safe(history: list[dict], question: str, hits: list[dict],
                         system_prompt: str | None):
    """LLM 生成包装：模型相关异常统一打上 LLMError 标记再上抛"""
    try:
        async for piece in stream_llm(history, question, hits, system_prompt):
            yield piece
    except Exception as e:   # noqa: BLE001
        raise LLMError(f"大模型调用失败：{e}") from e


def build_chat_response(req: ChatRequest) -> StreamingResponse:
    """/chat 路由的出口：解析/新建会话 → 用户问题先落库 → 返回 SSE 流

    会话只在这里解析一次，生成器直接复用，避免 ID 不一致。
    """
    if req.session_id:
        session_id = req.session_id
    else:
        session_id = db.create_session(db.DEMO_USER_ID, req.question)

    # 用户问题先落库：即使后续流式失败，历史里也保留员工问过什么
    # 批4：落库同时跑违规扫描（命中规则则把标记写入 meta.violation，审计页可筛）
    violation = scan_violation(req.question)
    db.add_message(session_id, "user", req.question,
                   meta={"violation": violation} if violation else None)

    return StreamingResponse(
        chat_generator(session_id, req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",     # nginx 场景下关闭缓冲
            "Connection": "keep-alive",
        },
    )
