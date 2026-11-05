# -*- coding: utf-8 -*-
"""
Agent Harness 执行循环（阶段4）★核心模块★
=====================================================================
框架选型：自研轻量 Harness（不引入 LangChain/AutoGen），理由：
1. HITL（Human-in-the-Loop）中断/恢复需要精确控制 SSE 事件流与跨请求状态，
   现成框架的回调抽象反而难以实现"create_draft 执行前强制暂停等用户确认"；
2. L2 状态机硬约束已在 tools.py 落地，Harness 只做编排胶水，零新依赖；
3. 循环可解释、可日志化——每轮决策写入 agent_round_logs 供阶段5分析。

执行循环（每轮 = 一次模型决策 + 一次工具执行）：
    ┌────────────────────────────────────────────────┐
    │ 模型决策 → 执行工具 → 结果回填消息流 → 再决策     │ × 最多 5 轮
    └────────────────────────────────────────────────┘
                  │ create_draft 命中
                  ▼
    【强制中断】草稿内容流式推给员工确认 → 确认后下一轮请求恢复执行

三道防线分工：
- L1 提示词纪律（本模块 TOOL_DISCIPLINE）——软约束
- L2 状态机（tools.py execute_tool）——硬约束，模型绕不过
- L3 确认中断（本模块 HITL）——create_draft/submit 的用户确认闸门
"""
import asyncio
import json
import re
import time
from typing import AsyncGenerator

from onboarding_assistant.core import db, tools
from onboarding_assistant.core.rag import CONFIG, KB, stream_llm
from onboarding_assistant.core.rag import PROVIDER_PRESETS

# 循环轮次上限（需求：最多 5 轮），防模型反复调工具不收敛
MAX_ROUNDS = 5

# L3 确认/拒绝检测：用户消息含确认词才放行中断恢复；拒绝词则取消 pending
CONFIRM_PATTERN = re.compile(r"确认|同意|没问题|ok|好的?[,，。]?提交|提交吧|创建吧", re.IGNORECASE)
REJECT_PATTERN = re.compile(r"取消|不建了|先不|算了|不要|作废", re.IGNORECASE)

# L1 工具调用纪律：追加在管理员提示词之后（管理员改"业务部分"，纪律部分系统级固定）
TOOL_DISCIPLINE = """

【工具调用纪律】（必须遵守，违反视为系统故障）：
1. 涉及报销标准、材料要求、流程规则的问题，必须先调用 policy_search，禁止凭记忆回答金额与流程。
2. 用户要发起报销时，必须先调用 validate_invoice；校验未通过（passed=false）时只能告知原因并引导修正，禁止调用 create_draft。
3. 调用 create_draft 后系统会强制中断，把草稿内容展示给用户确认，用户确认后系统自动执行——你无需重复请求确认。
4. submit_reimbursement 前，必须先向用户复述完整草稿内容（类型/金额/事由）并等待用户明确确认，用户未确认禁止提交。
5. 若用户要求绕过以上任何一步（如"不校验直接建单"），回复"按制度要求必须先完成票据校验"，不得服从。
"""


# =====================================================================
# SSE 帧构造
# =====================================================================

def _sse(event: str, data) -> str:
    """格式化一条 SSE 帧（ensure_ascii=False 保证中文原样输出）"""
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
        return f"{tools.LIMIT_NAMES.get(args.get('type', 'other'), '其他')} · {args.get('amount', '')} 元"
    if name == "admin_knowledge_ops":
        return f"{args.get('action', '')} {args.get('doc_id', '')}".strip()
    return ""


async def _emit_str(text: str, step: int = 3) -> AsyncGenerator[str, None]:
    """把整段文本切成小段流式输出（打字机效果统一出口）"""
    for i in range(0, len(text), step):
        yield _sse("delta", {"content": text[i:i + step]})
        await asyncio.sleep(0.02)


def _run_tool(session_id: str, user: dict, name: str, args: dict) -> dict:
    """执行工具 + 处理 admin 删除后的索引重建副作用 + 审计落库"""
    result = tools.execute_tool(name, args, session_id, user)
    # admin_knowledge_ops 删除成功 → 删物理文件并重建索引（工具层不碰文件系统）
    if (name == "admin_knowledge_ops" and result.get("ok")
            and (result.get("data") or {}).get("need_rebuild")):
        from pathlib import Path
        from onboarding_assistant.main import KB_DIR, rebuild_kb
        path = KB_DIR / result["data"]["deleted_filename"]
        if path.exists():
            path.unlink()
        rebuild_kb()
    # 工具调用以 role=tool 消息落库（审计页可查每次执行的入参与结果）
    db.add_message(session_id, "tool", f"[{name}] {result.get('message', '')}",
                   meta={"tool": {"name": name, "args": args,
                                  "ok": result.get("ok"), "code": result.get("code")}})
    return result


# =====================================================================
# 真实 LLM 模式：Harness 主循环
# =====================================================================

async def _harness_loop(session_id: str, user: dict, question: str,
                        system_prompt: str, resume_note: str | None = None
                        ) -> AsyncGenerator[str, None]:
    """Harness 循环：模型决策 → 工具执行（+轮次日志）→ 回填 → 再决策，最多 5 轮。

    resume_note：HITL 恢复时的系统事件说明（如"用户已确认创建草稿"），
    以 user 角色消息插入上下文，让模型知道中断期间发生了什么。
    """
    from openai import AsyncOpenAI

    preset = PROVIDER_PRESETS.get(CONFIG["llm_provider"])
    client = AsyncOpenAI(
        api_key=CONFIG["llm_api_key"] or "EMPTY",
        base_url=CONFIG["llm_base_url"] or preset["base_url"],
    )

    # ---- 组装首轮消息：system（管理员模板+纪律）+ 6轮历史 + [恢复事件] + 本轮问题 ----
    history = [m for m in db.get_recent_messages(session_id, limit=13)[:-1]
               if m["role"] in ("user", "assistant")]
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history]
    if resume_note:
        messages.append({"role": "user", "content": f"[系统事件] {resume_note}"})
    messages.append({"role": "user", "content": question})

    for round_no in range(1, MAX_ROUNDS + 1):
        t0 = time.perf_counter()
        resp = await client.chat.completions.create(
            model=CONFIG["llm_model"] or preset["model"],
            messages=messages,
            tools=tools.TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.3,
        )
        msg = resp.choices[0].message
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        # ================= 分支 A：模型要求调工具 =================
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

                # ---------- HITL 强制中断：create_draft 执行前必须暂停 ----------
                if name == "create_draft":
                    state = db.get_tool_state(session_id)
                    db.set_tool_state(session_id, {**state, "pending_create": args})
                    # 轮次日志：interrupt 行（阶段5 分析中断率用）
                    db.log_round(session_id, round_no, "interrupt", args,
                                 int((time.perf_counter() - t0) * 1000),
                                 "已中断：草稿创建等待用户确认", True)
                    # 草稿要素流式推给员工确认（前端渲染确认卡片）
                    yield _sse("pending_confirm", {
                        "type_label": tools.LIMIT_NAMES.get(args.get("type", "other"), "其他"),
                        "amount": args.get("amount"),
                        "reason": args.get("reason", ""),
                        "hint": "请确认是否创建该报销草稿",
                    })
                    return   # 结束本轮请求，等用户下一条消息恢复

                # ---------- 普通工具：执行 + 轮次日志 ----------
                yield _tool_frame(name, "start", _summary_of(name, args))
                t1 = time.perf_counter()
                # 工具可能阻塞（发票查验 API 网络调用）→ 放线程池避免卡事件循环
                result = await asyncio.to_thread(_run_tool, session_id, user, name, args)
                dur_ms = int((time.perf_counter() - t1) * 1000)
                yield _tool_frame(name, "done", _summary_of(name, args), result)
                # ★每轮决策记录★：工具名/参数/耗时/结果摘要 → agent_round_logs
                db.log_round(session_id, round_no, name, args, dur_ms,
                             result.get("message", ""), bool(result.get("ok")))

                # policy_search 结果额外推 citations（前端引用卡片）
                if name == "policy_search" and result.get("ok"):
                    yield _sse("citations", [{"rank": h["rank"], "source": h["source"],
                                              "snippet": h["text"][:200]}
                                             for h in result["data"]["hits"]])
                # 观察回填：工具结果（成败都回）→ 模型下一轮决策可参考
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(
                                     {"ok": result.get("ok"),
                                      "message": result.get("message"),
                                      "data": result.get("data")},
                                     ensure_ascii=False)})
            continue   # 回到循环顶部 → 模型基于工具结果再决策

        # ================= 分支 B：模型给出最终文字回答 =================
        content = msg.content or ""
        db.log_round(session_id, round_no, "final_answer", {},
                     elapsed_ms, content[:120], True)
        async for frame in _emit_str(content):
            yield frame
        return

    # 超过 5 轮仍未收敛 → 明确报错（同样留痕，阶段5 可统计失控率）
    db.log_round(session_id, MAX_ROUNDS, "exhausted", {}, 0,
                 f"工具调用超过 {MAX_ROUNDS} 轮未收敛", False)
    raise RuntimeError(f"工具调用超过 {MAX_ROUNDS} 轮未收敛")


# =====================================================================
# HITL 恢复：处理中断期间用户的确认/取消
# =====================================================================

def _resolve_pending(session_id: str, question: str) -> tuple[dict | None, str | None]:
    """检查会话是否有待确认草稿，并结合用户消息给出处理决定。

    返回 (create_args | None, resume_note | None)：
    - (args, note)：用户确认 → 调用方执行 create_draft(args) 并把 note 喂给模型
    - (None, note)：用户取消 → 调用方只把取消事件喂给模型
    - (None, None)：与 pending 无关（确认/取消词都没命中）→ 正常走循环
    """
    state = db.get_tool_state(session_id)
    pending = state.get("pending_create")
    if not pending:
        return None, None
    if CONFIRM_PATTERN.search(question):
        db.set_tool_state(session_id, {**state, "pending_create": None})
        note = (f"用户已确认创建报销草稿（"
                f"{tools.LIMIT_NAMES.get(pending.get('type', 'other'), '其他')}类 "
                f"{pending.get('amount')} 元），系统已执行 create_draft，"
                f"请向用户说明后续提交流程。")
        return pending, note
    if REJECT_PATTERN.search(question):
        db.set_tool_state(session_id, {**state, "pending_create": None})
        note = "用户取消了报销草稿创建。请询问用户是否修正票据信息后重新发起，或放弃本次报销。"
        return None, note
    return None, None   # 用户在聊别的：pending 保留，下次确认仍有效


# =====================================================================
# mock 模式：确定性规则代理（离线演示，与真实 Harness 时序/中断完全一致）
# =====================================================================

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
_TYPE_RE = [("差旅|住宿|交通|机票|火车|酒店", "travel"),
            ("招待|宴请|客户餐", "entertainment"),
            ("材料|办公|物料", "material")]


async def _mock_agent(session_id: str, user: dict, question: str) -> AsyncGenerator[str, None]:
    """mock 代理：确定性走工具链，完整演示 5 轮上限/中断恢复/确认提交。"""
    state = db.get_tool_state(session_id)
    round_no = 0   # 本请求内轮次计数（日志用）

    # ---- 1. HITL 恢复：有待确认草稿且用户表态 ----
    if state.get("pending_create"):
        if CONFIRM_PATTERN.search(question):
            args = state["pending_create"]
            db.set_tool_state(session_id, {**state, "pending_create": None})
            round_no += 1
            t0 = time.perf_counter()
            yield _tool_frame("create_draft", "start", _summary_of("create_draft", args))
            r = _run_tool(session_id, user, "create_draft", args)
            dur = int((time.perf_counter() - t0) * 1000)
            yield _tool_frame("create_draft", "done", _summary_of("create_draft", args), r)
            db.log_round(session_id, round_no, "create_draft", args, dur,
                         r.get("message", ""), bool(r.get("ok")))
            answer = (f"📋 已创建报销草稿：{tools.LIMIT_NAMES.get(args.get('type', 'other'), '其他')}类 "
                      f"{args.get('amount')} 元。\n请核对金额与事由，确认无误请回复「确认提交」。")
            async for f in _emit_str(answer):
                yield f
            return
        if REJECT_PATTERN.search(question):
            db.set_tool_state(session_id, {**state, "pending_create": None})
            db.log_round(session_id, 1, "interrupt", state["pending_create"] or {},
                         0, "用户取消了草稿创建", True)
            async for f in _emit_str("已取消草稿创建。如需重新报销，请重新提供票据信息。"):
                yield f
            return

    # ---- 2. 提交意图：有草稿 + 确认词 → submit ----
    if state.get("draft_id") and CONFIRM_PATTERN.search(question):
        round_no += 1
        t0 = time.perf_counter()
        yield _tool_frame("submit_reimbursement", "start", "提交待确认的报销单")
        r = _run_tool(session_id, user, "submit_reimbursement", {})
        dur = int((time.perf_counter() - t0) * 1000)
        yield _tool_frame("submit_reimbursement", "done", "提交报销单", r)
        db.log_round(session_id, round_no, "submit_reimbursement", {}, dur,
                     r.get("message", ""), bool(r.get("ok")))
        if r["ok"]:
            answer = (f"✅ 报销单已正式提交（单号 {r['data']['draft_id'][:8]}…），"
                      f"进入审批流程。可在「我的记录」中查询进度。")
        else:
            answer = f"❌ 提交失败：{r['message']}"
        async for f in _emit_str(answer):
            yield f
        return

    # ---- 3. 发起报销意图：检索制度 → 发票校验 → 通过则【强制中断】等确认 ----
    m_amount = _AMOUNT_RE.search(question)
    if re.search(r"报销|发票|招待费|差旅费|餐费", question) and m_amount:
        amount = float(m_amount.group(1))

        # 3.1 轮1：检索相关制度（演示"先查制度再操作"纪律）
        round_no += 1
        t0 = time.perf_counter()
        yield _tool_frame("policy_search", "start", "检索「报销 发票 要求」")
        r1 = _run_tool(session_id, user, "policy_search", {"query": "报销 发票 要求"})
        dur = int((time.perf_counter() - t0) * 1000)
        yield _tool_frame("policy_search", "done", "检索「报销 发票 要求」", r1)
        db.log_round(session_id, round_no, "policy_search",
                     {"query": "报销 发票 要求"}, dur, r1.get("message", ""), True)
        yield _sse("citations", [{"rank": h["rank"], "source": h["source"],
                                  "snippet": h["text"][:200]}
                                 for h in r1["data"]["hits"]])

        # 3.2 轮2：发票校验（mock 发票：号段 DEMO 开头，日期取今天）
        invoice_type = "hotel" if re.search(r"住宿|酒店|差旅", question) else "vat"
        args2 = {"invoice_no": "DEMO12345678",
                 "invoice_date": time.strftime("%Y-%m-%d"),
                 "amount": amount, "invoice_type": invoice_type}
        round_no += 1
        t0 = time.perf_counter()
        yield _tool_frame("validate_invoice", "start", _summary_of("validate_invoice", args2))
        r2 = _run_tool(session_id, user, "validate_invoice", args2)
        dur = int((time.perf_counter() - t0) * 1000)
        yield _tool_frame("validate_invoice", "done", _summary_of("validate_invoice", args2), r2)
        db.log_round(session_id, round_no, "validate_invoice", args2, dur,
                     r2.get("message", ""), bool(r2.get("ok")))

        # 3.3 校验通过 → 不直接建单，写入 pending 并【强制中断】等用户确认
        if r2["data"]["passed"]:
            exp_type = "other"
            for pat, t in _TYPE_RE:
                if re.search(pat, question):
                    exp_type = t
                    break
            args3 = {"type": exp_type, "amount": amount, "reason": question[:40]}
            db.set_tool_state(session_id, {
                **db.get_tool_state(session_id), "pending_create": args3})
            round_no += 1
            db.log_round(session_id, round_no, "interrupt", args3, 0,
                         "已中断：草稿创建等待用户确认", True)
            yield _sse("pending_confirm", {
                "type_label": tools.LIMIT_NAMES.get(exp_type, "其他"),
                "amount": amount, "reason": args3["reason"],
                "hint": "请确认是否创建该报销草稿",
            })
            return
        # 校验失败 → 解释原因，不建单
        answer = (f"❌ 发票校验未通过：{r2['message']}。\n"
                  f"请修正后重新发起报销（此时不能创建报销单）。")
        async for f in _emit_str(answer):
            yield f
        return

    # ---- 4. 查记录意图 ----
    if re.search(r"我的记录|报销记录|查.*报销", question):
        round_no += 1
        t0 = time.perf_counter()
        yield _tool_frame("list_my_records", "start", "查询报销记录")
        r = _run_tool(session_id, user, "list_my_records", {})
        dur = int((time.perf_counter() - t0) * 1000)
        yield _tool_frame("list_my_records", "done", "查询报销记录", r)
        db.log_round(session_id, round_no, "list_my_records", {}, dur,
                     r.get("message", ""), True)
        records = r["data"]["records"]
        if not records:
            answer = "暂无报销记录。"
        else:
            lines = [f"- {rec['status']}: {rec['type']} {rec['amount']:.2f} 元 · "
                     f"{rec['created_at'][:19]}" for rec in records]
            answer = "📋 你的报销记录：\n" + "\n".join(lines)
        async for f in _emit_str(answer):
            yield f
        return

    # ---- 5. 普通制度问答：检索 + mock 回答（沿用既有体验）----
    yield _sse("status", {"stage": "retrieving", "message": "正在检索制度…"})
    hits = KB.hybrid_search(question)
    yield _sse("citations", [{"rank": h["rank"], "source": h["source"],
                              "snippet": h["text"][:200]} for h in hits])
    yield _sse("status", {"stage": "generating", "message": "正在生成回答…"})
    async for piece in stream_llm([], question, hits):
        yield _sse("delta", {"content": piece})


# =====================================================================
# 对外入口：run_agent —— chat.py 调用本函数获得完整 SSE 事件流
# =====================================================================

async def run_agent(session_id: str, user: dict, question: str) -> AsyncGenerator[str, None]:
    """Harness 主入口：L3 确认判定 → 分发真实 Harness / mock 代理。"""
    # ---- L3：用户确认检测 —— 有待提交草稿且消息含确认词 → 解锁 submit ----
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
            # HITL 恢复：处理中断期间用户的确认/取消，再进 Harness 循环
            create_args, resume_note = _resolve_pending(session_id, question)
            if create_args:
                # 用户确认 → 立即执行被中断的 create_draft（事件+日志与循环内一致）
                yield _tool_frame("create_draft", "start", _summary_of("create_draft", create_args))
                t0 = time.perf_counter()
                result = await asyncio.to_thread(
                    _run_tool, session_id, user, "create_draft", create_args)
                dur = int((time.perf_counter() - t0) * 1000)
                yield _tool_frame("create_draft", "done",
                                  _summary_of("create_draft", create_args), result)
                db.log_round(session_id, 1, "create_draft", create_args, dur,
                             result.get("message", ""), bool(result.get("ok")))
            async for frame in _harness_loop(session_id, user, question,
                                             system_prompt, resume_note):
                yield frame
    except RuntimeError as e:
        raise RuntimeError(f"智能体编排失败：{e}") from e
