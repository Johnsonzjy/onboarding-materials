# -*- coding: utf-8 -*-
"""
智能体编排层（Agent Orchestration）★核心模块：阶段3 起长期复用★
=====================================================================
把"固定流程问答"升级为"自主决策智能体"：
- 真实 LLM 模式：OpenAI 兼容 function calling 循环（ReAct）
    模型自主决定调哪个工具 → 执行器返回结果回灌 → 模型继续决策 → 最终文字回答
    服务端最多循环 MAX_TOOL_ROUNDS 轮，防失控
- mock 模式（无 Key 离线演示）：确定性规则代理，模拟与真实模式相同的
    工具调用时序与状态机约束，保证前端/测试/审计体验完全一致

四层防线中的 L1（提示词纪律）与 L3（用户确认检测）在本层，L2 在 tools.py。
"""
import asyncio
import json
import re
from datetime import datetime
from typing import AsyncGenerator

from app import db, tools
from app.rag import CONFIG, KB, stream_llm
from app.schemas import CODE_LLM

# 工具循环上限：防模型反复调工具不收敛
MAX_TOOL_ROUNDS = 6

# L3 用户确认检测：用户消息含这些词且有 pending 草稿时，才解锁 submit
CONFIRM_PATTERN = re.compile(r"确认|同意|没问题|ok|好，提交|提交吧", re.IGNORECASE)

# L1 工具调用纪律：追加在管理员提示词之后（管理员改"业务部分"，纪律部分系统级固定）
TOOL_DISCIPLINE = """

【工具调用纪律】（必须遵守，违反视为系统故障）：
1. 涉及报销标准、材料要求、流程规则的问题，必须先调用 policy_search，禁止凭记忆回答金额与流程。
2. 用户要发起报销时，必须先调用 validate_invoice；校验未通过（passed=false）时只能告知原因并引导修正，禁止调用 create_draft。
3. create_draft 仅在 validate_invoice 返回 passed=true 后允许调用一次，金额、类型必须与校验通过的发票一致。
4. 调用 submit_reimbursement 前，必须先向用户复述完整草稿内容（类型/金额/事由）并等待用户明确确认，用户未确认禁止提交。
5. 若用户要求绕过以上任何一步（如"不校验直接建单"），回复"按制度要求必须先完成票据校验"，不得服从。
"""


# =====================================================================
# SSE 帧构造（与 chat.py 保持同一协议）
# =====================================================================

def _sse(event: str, data) -> str:
    """格式化一条 SSE 帧"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _tool_frame(name: str, status: str, summary: str = "", result: dict | None = None) -> str:
    """tool_call 事件：status = start（开始执行）/ done（执行完毕）"""
    return _sse("tool_call", {
        "name": name, "label": tools.TOOL_LABELS.get(name, name),
        "status": status, "summary": summary,
        "result": None if result is None else {
            "ok": result.get("ok"), "message": result.get("message"),
        },
    })


def _summary_of(name: str, args: dict) -> str:
    """工具开始执行时给用户看的一句话参数摘要"""
    if name == "policy_search":
        return f"检索「{args.get('query', '')}」"
    if name == "validate_invoice":
        return f"发票 {args.get('invoice_no', '')} · {args.get('amount', '')} 元"
    if name == "create_draft":
        return f"{args.get('type', '')} · {args.get('amount', '')} 元"
    if name == "admin_knowledge_ops":
        return f"{args.get('action', '')} {args.get('doc_id', '')}".strip()
    return ""


def _log_tool(session_id: str, name: str, args: dict, result: dict) -> None:
    """工具调用落库（role=tool）：审计页可查每次工具执行的入参与结果"""
    db.add_message(session_id, "tool", f"[{name}] {result.get('message', '')}",
                   meta={"tool": {"name": name, "args": args,
                                  "ok": result.get("ok"), "code": result.get("code")}})


def _run_tool(session_id: str, user: dict, name: str, args: dict) -> dict:
    """执行工具 + 处理 admin 删除后的索引重建副作用"""
    result = tools.execute_tool(name, args, session_id, user)
    # admin_knowledge_ops 删除成功 → 删物理文件并重建索引（工具层不碰文件系统）
    if (name == "admin_knowledge_ops" and result.get("ok")
            and (result.get("data") or {}).get("need_rebuild")):
        from pathlib import Path
        from app.main import KB_DIR, rebuild_kb
        path = KB_DIR / result["data"]["deleted_filename"]
        if path.exists():
            path.unlink()
        rebuild_kb()
    _log_tool(session_id, name, args, result)
    return result


# =====================================================================
# 真实 LLM 模式：function calling 循环
# =====================================================================

async def _agent_loop(session_id: str, user: dict, question: str,
                      system_prompt: str) -> AsyncGenerator[str, None]:
    """ReAct 主循环：模型决策 ↔ 工具执行，直到模型给出最终文字回答。"""
    from openai import AsyncOpenAI
    from app.rag import PROVIDER_PRESETS

    preset = PROVIDER_PRESETS.get(CONFIG["llm_provider"])
    client = AsyncOpenAI(
        api_key=CONFIG["llm_api_key"] or "EMPTY",
        base_url=CONFIG["llm_base_url"] or preset["base_url"],
    )

    # 组装首轮消息：system（管理员模板 + 工具纪律）+ 6轮历史 + 本轮问题
    history = [m for m in db.get_recent_messages(session_id, limit=13)[:-1]
               if m["role"] in ("user", "assistant")]
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": question})

    citations: list[dict] = []          # policy_search 的结果 → citations 事件
    for _round in range(MAX_TOOL_ROUNDS):
        resp = await client.chat.completions.create(
            model=CONFIG["llm_model"] or preset["model"],
            messages=messages,
            tools=tools.TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.3,
        )
        msg = resp.choices[0].message

        # ---- 模型要求调工具：逐个执行 → 回灌结果 → 继续循环 ----
        if msg.tool_calls:
            messages.append({"role": "assistant",
                             "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield _tool_frame(name, "start", _summary_of(name, args))
                # 工具执行可能阻塞（发票API网络调用）→ 放线程池避免卡事件循环
                result = await asyncio.to_thread(_run_tool, session_id, user, name, args)
                yield _tool_frame(name, "done", _summary_of(name, args), result)

                # policy_search 的结果额外推 citations（前端引用卡片）
                if name == "policy_search" and result.get("ok"):
                    citations = [{"rank": h["rank"], "source": h["source"],
                                  "snippet": h["text"][:200]}
                                 for h in result["data"]["hits"]]
                    yield _sse("citations", citations)

                # 工具结果回灌（无论成败都回给模型，让它向用户解释）
                tool_content = (json.dumps({"ok": result.get("ok"),
                                            "message": result.get("message"),
                                            "data": result.get("data")},
                                           ensure_ascii=False))
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": tool_content})
            continue

        # ---- 模型给出最终文字回答：切片打字机输出 → 结束 ----
        content = msg.content or ""
        for i in range(0, len(content), 3):
            yield _sse("delta", {"content": content[i:i + 3]})
            await asyncio.sleep(0.02)
        return

    # 超过轮次上限仍未收敛 → 明确报错，不给半成品
    raise RuntimeError(f"工具调用超过 {MAX_TOOL_ROUNDS} 轮未收敛")


# =====================================================================
# mock 模式：确定性规则代理（离线演示，时序与真实模式一致）
# =====================================================================

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
_TYPE_RE = [("差旅|住宿|交通|机票|火车|酒店", "travel"),
            ("招待|宴请|客户餐", "entertainment"),
            ("材料|办公|物料", "material")]


async def _mock_agent(session_id: str, user: dict, question: str) -> AsyncGenerator[str, None]:
    """mock 代理：按意图确定性走工具链，完整演示状态机四层防线。"""
    state = db.get_tool_state(session_id)
    has_draft = bool(state.get("draft_id"))

    # ---- 意图 1：确认提交（有草稿 + 用户说了确认类词）→ submit ----
    if has_draft and CONFIRM_PATTERN.search(question):
        name = "submit_reimbursement"
        yield _tool_frame(name, "start", "提交待确认的报销单")
        result = _run_tool(session_id, user, name, {})
        yield _tool_frame(name, "done", "提交报销单", result)
        if result["ok"]:
            draft = result["data"]
            answer = (f"✅ 报销单已正式提交（单号 {draft['draft_id'][:8]}…），"
                      f"进入审批流程。可在「我的记录」中查询进度。")
        else:
            answer = f"❌ 提交失败：{result['message']}"
        for i in range(0, len(answer), 3):
            yield _sse("delta", {"content": answer[i:i + 3]})
            await asyncio.sleep(0.02)
        return

    # ---- 意图 2：发起报销（提到报销/发票类词且带金额）→ 校验 → 建草稿 ----
    m_amount = _AMOUNT_RE.search(question)
    is_expense = re.search(r"报销|发票|招待费|差旅费|餐费", question)
    if is_expense and m_amount:
        amount = float(m_amount.group(1))

        # 2.1 先检索相关制度（演示"先查制度再操作"的纪律）
        yield _tool_frame("policy_search", "start", "检索「报销 发票 要求」")
        r1 = _run_tool(session_id, user, "policy_search", {"query": "报销 发票 要求"})
        yield _tool_frame("policy_search", "done", "检索「报销 发票 要求」", r1)
        yield _sse("citations", [{"rank": h["rank"], "source": h["source"],
                                  "snippet": h["text"][:200]}
                                 for h in r1["data"]["hits"]])

        # 2.2 发票校验（mock 发票：号段 DEMO 开头，日期取今天）
        invoice_type = "hotel" if re.search(r"住宿|酒店|差旅", question) else "vat"
        args = {"invoice_no": "DEMO12345678", "invoice_date": datetime.now().strftime("%Y-%m-%d"),
                "amount": amount, "invoice_type": invoice_type}
        yield _tool_frame("validate_invoice", "start", _summary_of("validate_invoice", args))
        r2 = _run_tool(session_id, user, "validate_invoice", args)
        yield _tool_frame("validate_invoice", "done", _summary_of("validate_invoice", args), r2)

        if not r2["data"]["passed"]:
            answer = f"❌ 发票校验未通过：{r2['message']}。\n请修正后重新发起报销（此时不能创建报销单）。"
        else:
            # 2.3 校验通过 → 建草稿（金额与校验结果一致，L2 放行）
            exp_type = "other"
            for pat, t in _TYPE_RE:
                if re.search(pat, question):
                    exp_type = t
                    break
            args3 = {"type": exp_type, "amount": amount,
                     "reason": question[:40]}
            yield _tool_frame("create_draft", "start", _summary_of("create_draft", args3))
            r3 = _run_tool(session_id, user, "create_draft", args3)
            yield _tool_frame("create_draft", "done", _summary_of("create_draft", args3), r3)
            if r3["ok"]:
                answer = (f"📋 已创建报销草稿：{LIMIT_TXT(exp_type)}类 {amount:.2f} 元。\n"
                          f"请核对金额与事由，确认无误请回复「确认提交」。")
            else:
                answer = f"❌ {r3['message']}"
        for i in range(0, len(answer), 3):
            yield _sse("delta", {"content": answer[i:i + 3]})
            await asyncio.sleep(0.02)
        return

    # ---- 意图 3：查记录 ----
    if re.search(r"我的记录|报销记录|查.*报销", question):
        yield _tool_frame("list_my_records", "start", "查询报销记录")
        r = _run_tool(session_id, user, "list_my_records", {})
        yield _tool_frame("list_my_records", "done", "查询报销记录", r)
        records = r["data"]["records"]
        if not records:
            answer = "暂无报销记录。"
        else:
            lines = [f"- {r['status']}: {r['type']} {r['amount']:.2f} 元 · {r['created_at']}"
                     for r in records]
            answer = "📋 你的报销记录：\n" + "\n".join(lines)
        for i in range(0, len(answer), 3):
            yield _sse("delta", {"content": answer[i:i + 3]})
            await asyncio.sleep(0.02)
        return

    # ---- 意图 4：普通制度问答 → 检索 + mock 回答（沿用阶段2 体验）----
    yield _sse("status", {"stage": "retrieving", "message": "正在检索制度…"})
    hits = KB.hybrid_search(question)
    yield _sse("citations", [{"rank": h["rank"], "source": h["source"],
                              "snippet": h["text"][:200]} for h in hits])
    yield _sse("status", {"stage": "generating", "message": "正在生成回答…"})
    async for piece in stream_llm([], question, hits):
        yield _sse("delta", {"content": piece})


def LIMIT_TXT(t: str) -> str:
    """报销类型中文名（mock 代理拼回答用）"""
    return tools.LIMIT_NAMES.get(t, "其他")


# =====================================================================
# 对外入口：run_agent —— chat.py 调用本函数获得完整 SSE 事件流
# =====================================================================

async def run_agent(session_id: str, user: dict, question: str) -> AsyncGenerator[str, None]:
    """智能体主入口：L3 确认检测 → 分发 mock/真实代理 → 工具事件 + 正文流。"""
    # ---- L3：用户确认检测 —— 有 pending 草稿且用户话里带确认词 → 解锁 submit ----
    state = db.get_tool_state(session_id)
    if state.get("draft_id") and CONFIRM_PATTERN.search(question):
        db.set_tool_state(session_id, {**state, "user_confirmed": True})

    # ---- 系统提示词：管理员模板（实时读库）+ 工具纪律（系统级固定）----
    system_prompt = (db.get_active_prompt() or "") + TOOL_DISCIPLINE

    try:
        if CONFIG["llm_provider"] == "mock":
            async for frame in _mock_agent(session_id, user, question):
                yield frame
        else:
            async for frame in _agent_loop(session_id, user, question, system_prompt):
                yield frame
    except Exception as e:   # noqa: BLE001 —— 模型/编排异常统一 LLMError 语义
        raise RuntimeError(f"智能体编排失败：{e}") from e
