# -*- coding: utf-8 -*-
"""
/chat 接口的 SSE 出口层（阶段3 起委托智能体编排层 agent.run_agent）

事件时序（协议不变，新增 tool_call 事件）：
  meta      → session_id（新建会话时告知前端）
  status    → "正在检索制度…" 等
  tool_call → 工具调用过程（start / done，含中文摘要与结果 ok/message）
  citations → 引用片段列表（policy_search 结果）
  delta     → 正文增量（逐段，前端打字机）
  done      → 结束信号（session_id + message_id）
  error     → 流内错误（code + 中文 message）

会话 ID 只在 build_chat_response 中解析一次并传给生成器，保证前后一致。
"""
import json
from typing import AsyncGenerator

from fastapi.responses import StreamingResponse

from app import db
from app.audit import scan_violation
from app.agent import run_agent
from app.schemas import CODE_INTERNAL, CODE_LLM, ChatRequest


def _sse(event: str, data) -> str:
    """格式化一条 SSE 帧（ensure_ascii=False 保证中文原样输出）"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def chat_generator(session_id: str, user: dict,
                         req: ChatRequest) -> AsyncGenerator[str, None]:
    """SSE 主流程：告知会话 → 智能体编排（工具循环/流式回答）→ 落库"""
    # ---- 1. 先把会话 ID 告知前端（新建会话时前端据此保存） ----
    yield _sse("meta", {"session_id": session_id})

    full_answer: list[str] = []
    try:
        # ---- 2. 委托智能体编排层：工具调用事件 + 正文 delta 全部透传 ----
        async for frame in run_agent(session_id, user, req.question):
            # 捕获 delta 内容用于落库（其余帧原样透传）
            if frame.startswith("event: delta"):
                try:
                    payload = json.loads(frame.split("data: ", 1)[1].strip())
                    full_answer.append(payload.get("content", ""))
                except (IndexError, json.JSONDecodeError):
                    pass
            yield frame

        # ---- 3. 助手回答落库（工具调用已在 agent 层以 role=tool 落库）----
        db.add_message(session_id, "assistant", "".join(full_answer))
        db.touch_session(session_id)
        yield _sse("done", {"session_id": session_id})

    except RuntimeError as e:
        # 智能体编排失败（模型不可达/超时/轮次超限）→ 3001
        yield _sse("error", {"code": CODE_LLM, "message": f"大模型调用失败，请稍后重试（{e}）"})
    except Exception:   # noqa: BLE001 —— 流内其余异常 → 5000
        yield _sse("error", {"code": CODE_INTERNAL, "message": "系统内部错误，请稍后重试"})


def build_chat_response(req: ChatRequest, user: dict) -> StreamingResponse:
    """/chat 路由的出口：解析/新建会话 → 用户问题先落库 → 返回 SSE 流

    会话只在这里解析一次，生成器直接复用，避免 ID 不一致。
    """
    if req.session_id:
        session_id = req.session_id
    else:
        session_id = db.create_session(user["user_id"], req.question)

    # 用户问题先落库：即使后续流式失败，历史里也保留员工问过什么
    # 落库同时跑违规扫描（命中规则则把标记写入 meta.violation，审计页可筛）
    violation = scan_violation(req.question)
    db.add_message(session_id, "user", req.question,
                   meta={"violation": violation} if violation else None)

    return StreamingResponse(
        chat_generator(session_id, user, req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",     # nginx 场景下关闭缓冲
            "Connection": "keep-alive",
        },
    )
