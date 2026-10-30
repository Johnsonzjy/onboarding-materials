# -*- coding: utf-8 -*-
"""
智能体工具层（Function Calling）★核心模块：阶段3 起长期复用★
=====================================================================
职责：
1. TOOL_SCHEMAS —— 6 个工具的 OpenAI function calling 规范（给 LLM 看的说明书）
2. execute_tool —— 统一工具执行器：内置 L2 硬约束状态机
   （create_draft 必须在 validate_invoice 通过后；submit 必须有草稿 + 用户确认）
3. validate_invoice —— 发票校验：规则引擎 + 可配置的第三方查验 API 适配器
   （INVOICE_API_PROVIDER=generic 时走真实 API；为空时仅规则校验，离线可跑）

设计原则（对应需求评审结论）：
- 顺序约束的真正执行点在"服务端工具执行器"（L2），不依赖提示词（L1）——模型骗不过；
- 每个工具的执行结果都返回 {ok, message, data}，编排层把结果回灌给 LLM 或直接渲染。
"""
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from urllib import request as _urlrequest
from urllib.error import URLError

from app import db
from app.rag import KB

# =====================================================================
# 发票查验 API 配置（环境变量可覆盖；默认不接 API，仅规则校验）
# ---------------------------------------------------------------------
# INVOICE_API_PROVIDER: ""=不接API（仅规则） | "generic"=通用 REST 查验接口
# INVOICE_API_URL:      查验接口地址（POST JSON）
# INVOICE_API_KEY:      查验接口鉴权 Key（放 Authorization: Bearer 头）
# 通用适配器约定：请求体 {invoice_no, invoice_date, amount, check_code}，
#                响应 JSON 中任意布尔字段 valid/valid/pass/passed/success 为真即通过。
#                对接具体供应商（百望/航信/票易通等）时按其文档调整 _call_invoice_api。
# =====================================================================
INVOICE_API_PROVIDER = os.getenv("INVOICE_API_PROVIDER", "")
INVOICE_API_URL = os.getenv("INVOICE_API_URL", "")
INVOICE_API_KEY = os.getenv("INVOICE_API_KEY", "")

# 各报销类型的金额上限（硬卡点；生产环境应从制度知识库/配置中心读取）
TYPE_LIMITS = {
    "travel": 5000.0,          # 差旅
    "entertainment": 2000.0,   # 业务招待
    "material": 10000.0,       # 入职材料/办公物料
    "other": 1000.0,           # 其他
}
LIMIT_NAMES = {
    "travel": "差旅", "entertainment": "业务招待",
    "material": "入职材料", "other": "其他",
}
# 发票类型 → 报销类型归并（校验时按归并后的类型卡上限）
INVOICE_TO_EXPENSE = {
    "vat": "other", "hotel": "travel", "train": "travel",
    "taxi": "travel", "meal_entertainment": "entertainment", "other": "other",
}


# =====================================================================
# 一、工具规范（OpenAI function calling 格式，直接传给 LLM 的 tools 参数）
# =====================================================================

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "policy_search",
            "description": "检索公司报销/入职制度条文。凡涉及报销标准、材料要求、流程规则的问题，必须先调用本工具，禁止凭记忆回答金额与流程。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词，如'差旅住宿标准'"},
                    "category": {"type": "string", "enum": ["travel", "entertainment", "material", "all"],
                                 "description": "制度分类，不确定时用 all"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_invoice",
            "description": "校验发票/票据：格式、开票日期30天内、金额是否超该类目报销上限，并（如已配置）调用第三方发票查验接口验真。发起报销前必须先通过本工具校验。",
            "parameters": {
                "type": "object",
                "properties": {
                    "invoice_no": {"type": "string", "description": "发票号码（8位以上数字/字母）"},
                    "invoice_date": {"type": "string", "description": "开票日期，格式 YYYY-MM-DD"},
                    "amount": {"type": "number", "description": "发票金额（元）"},
                    "invoice_type": {"type": "string",
                                     "enum": ["vat", "hotel", "train", "taxi", "meal_entertainment", "other"],
                                     "description": "发票类型"},
                    "check_code": {"type": "string", "description": "校验码（可选，增值税发票查验用）"}
                },
                "required": ["invoice_no", "invoice_date", "amount", "invoice_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_draft",
            "description": "创建报销单草稿。前置条件（服务端强制）：本会话必须已通过 validate_invoice 且通过，金额、类型须与校验结果一致，否则调用会被拒绝。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["travel", "entertainment", "material", "other"],
                             "description": "报销类型"},
                    "amount": {"type": "number", "description": "报销金额（元），必须与校验通过的发票金额一致"},
                    "reason": {"type": "string", "description": "报销事由（一句话）"},
                    "items": {"type": "array", "description": "明细行，可空",
                              "items": {"type": "object", "properties": {
                                  "name": {"type": "string"}, "amount": {"type": "number"}}}}
                },
                "required": ["type", "amount", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_reimbursement",
            "description": "提交报销单（正式提交）。前置条件（服务端强制）：本会话已有草稿，且用户已明确确认金额。提交前必须先向用户复述草稿内容并等待确认。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_records",
            "description": "查询当前员工自己的报销单记录（草稿/已提交）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "admin_knowledge_ops",
            "description": "管理员专用：查看制度文档列表或删除指定制度文档。仅管理员角色可用，员工调用会被拒绝。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "delete"], "description": "操作类型"},
                    "doc_id": {"type": "string", "description": "删除时的文档 ID（list 时忽略）"}
                },
                "required": ["action"],
            },
        },
    },
]

# 工具中文名（前端展示用）
TOOL_LABELS = {
    "policy_search": "检索制度",
    "validate_invoice": "发票校验",
    "create_draft": "创建报销草稿",
    "submit_reimbursement": "提交报销单",
    "list_my_records": "查询报销记录",
    "admin_knowledge_ops": "知识库管理",
}


# =====================================================================
# 二、发票查验 API 适配器
# =====================================================================

def _call_invoice_api(args: dict) -> dict:
    """调用第三方发票查验接口（generic 通用适配器）。

    返回 {called: bool, valid: bool|None, message: str}。
    网络异常/响应异常一律按"查验不通过"处理（fail-closed，宁可拦截不可放行）。
    """
    if INVOICE_API_PROVIDER != "generic" or not INVOICE_API_URL:
        return {"called": False, "valid": None, "message": "未配置查验API，仅规则校验"}
    payload = json.dumps({
        "invoice_no": args.get("invoice_no", ""),
        "invoice_date": args.get("invoice_date", ""),
        "amount": args.get("amount"),
        "check_code": args.get("check_code", ""),
    }).encode("utf-8")
    req = _urlrequest.Request(
        INVOICE_API_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INVOICE_API_KEY}"},
    )
    try:
        with _urlrequest.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        return {"called": True, "valid": False, "message": f"查验服务不可用：{e}"}
    # 宽松解析：兼容 valid/valid_/pass/passed/success 等常见字段名
    valid = None
    for key in ("valid", "passed", "pass", "success", "is_valid"):
        if isinstance(body.get(key), bool):
            valid = body[key]
            break
    if valid is None:
        return {"called": True, "valid": False, "message": "查验响应格式无法识别"}
    return {"called": True, "valid": valid,
            "message": "第三方查验通过" if valid else "第三方查验不通过"}


def _check_invoice_rules(args: dict) -> dict:
    """发票规则引擎：格式 / 日期30天内 / 金额上限，返回逐项校验结果。"""
    checks: dict[str, str] = {}

    # 1. 发票号格式：≥8 位数字/字母
    no = (args.get("invoice_no") or "").strip()
    checks["invoice_no"] = "通过" if re.fullmatch(r"[A-Za-z0-9]{8,}", no) else "不通过（发票号须为8位以上数字/字母）"

    # 2. 开票日期：可解析且在 30 天内
    try:
        d = datetime.strptime(args.get("invoice_date", ""), "%Y-%m-%d").date()
        diff = (date.today() - d).days
        checks["invoice_date"] = ("通过" if 0 <= diff <= 30
                                  else f"不通过（开票已 {diff} 天，超过 30 天报销时限）")
    except ValueError:
        checks["invoice_date"] = "不通过（日期格式须为 YYYY-MM-DD）"

    # 3. 金额：> 0 且不超过该类目上限
    amount = args.get("amount")
    category = INVOICE_TO_EXPENSE.get(args.get("invoice_type", "other"), "other")
    if not isinstance(amount, (int, float)) or amount <= 0:
        checks["amount"] = "不通过（金额必须为正数）"
        over_limit = False
    else:
        limit = TYPE_LIMITS[category]
        over_limit = amount > limit
        checks["amount"] = ("通过" if not over_limit
                            else f"不通过（{LIMIT_NAMES[category]}类单笔上限 {limit:.0f} 元，当前 {amount:.2f} 元）")

    rules_ok = all(v == "通过" for v in checks.values())
    return {"checks": checks, "rules_ok": rules_ok, "category": category}


def validate_invoice(args: dict) -> dict:
    """validate_invoice 工具实现：规则引擎 +（可选）第三方查验 API。

    返回 {ok, message, data:{passed, checks, api, amount, category}}。
    data.amount / data.category 会被写入会话状态，create_draft 的金额一致性
    校验以本结果为准（L2 硬约束的数据来源）。
    """
    rule = _check_invoice_rules(args)
    api = _call_invoice_api(args)

    # fail-closed：规则不过 或 API 已配置但验伪/不可用 → 一律不通过
    passed = rule["rules_ok"] and (not api["called"] or api["valid"] is True)
    if not rule["rules_ok"]:
        reason = "；".join(f"{k}:{v}" for k, v in rule["checks"].items() if v != "通过")
    elif api["called"] and api["valid"] is not True:
        reason = api["message"]
    else:
        reason = "全部校验通过"

    return {
        "ok": True,
        "message": reason,
        "data": {"passed": passed, "checks": rule["checks"], "api": api,
                 "amount": args.get("amount"), "category": rule["category"]},
    }


# =====================================================================
# 三、工具执行器（L2 硬约束状态机所在）
# =====================================================================

def _amount_equal(a, b) -> bool:
    """金额一致性比较（分位容差）"""
    try:
        return abs(float(a) - float(b)) < 0.01
    except (TypeError, ValueError):
        return False


def execute_tool(name: str, args: dict, session_id: str, user: dict) -> dict:
    """统一工具入口：分发执行 + L2 状态机校验。

    - 一切前置校验都在这里做，模型无法绕过（返回 ok=False 时编排层把
      中文错误回灌给模型，让模型向用户解释，而不是中断对话）。
    - user: {user_id, role, name}，来自 JWT。
    """
    state = db.get_tool_state(session_id)

    # ---------------- ① policy_search：无前置 ----------------
    if name == "policy_search":
        query = (args.get("query") or "").strip() or "报销制度"
        hits = KB.hybrid_search(query)
        return {"ok": True, "message": f"检索到 {len(hits)} 条相关制度条文",
                "data": {"hits": [{"rank": h["rank"], "source": h["source"],
                                   "text": h["text"]} for h in hits]}}

    # ---------------- ② validate_invoice：无前置，但结果写状态 ----------------
    if name == "validate_invoice":
        result = validate_invoice(args)
        # 校验结果落会话状态（create_draft 的前置依据）
        db.set_tool_state(session_id, {
            **state,
            "validated": {"passed": result["data"]["passed"],
                          "amount": result["data"]["amount"],
                          "category": result["data"]["category"]},
            "user_confirmed": False,       # 新一轮报销，用户确认态复位
        })
        return result

    # ---------------- ③ create_draft：L2 硬约束（必须先校验通过）----------------
    if name == "create_draft":
        validated = state.get("validated")
        if not validated or not validated.get("passed"):
            return {"ok": False, "code": 4002,
                    "message": "创建失败：本会话尚未通过发票校验，必须先调用 validate_invoice 并校验通过"}
        if not _amount_equal(args.get("amount"), validated.get("amount")):
            return {"ok": False, "code": 4003,
                    "message": (f"创建失败：报销金额 {args.get('amount')} 元与校验通过的发票金额 "
                                f"{validated.get('amount')} 元不一致")}
        amount = float(args["amount"])
        items = args.get("items") or []
        summary = (f"报销单草稿：{LIMIT_NAMES.get(args.get('type'), '其他')}类 "
                   f"{amount:.2f} 元，事由：{args.get('reason', '')}")
        draft = db.insert_draft(session_id, user["user_id"], args.get("type", "other"),
                                amount, args.get("reason", ""), items, summary)
        db.set_tool_state(session_id, {**state, "draft_id": draft["id"], "user_confirmed": False})
        return {"ok": True, "message": summary + "（待用户确认后提交）",
                "data": {"draft_id": draft["id"], "amount": amount}}

    # ---------------- ④ submit_reimbursement：L2 硬约束（草稿 + 用户确认）----------------
    if name == "submit_reimbursement":
        draft_id = state.get("draft_id")
        if not draft_id:
            return {"ok": False, "code": 4002,
                    "message": "提交失败：当前会话没有待提交的报销草稿"}
        if not state.get("user_confirmed"):
            return {"ok": False, "code": 4004,
                    "message": "提交失败：用户尚未确认草稿内容，必须先复述金额与事由并等待用户确认"}
        db.set_draft_submitted(draft_id)
        db.set_tool_state(session_id, {**state, "draft_id": None, "submitted": True})
        return {"ok": True, "message": "报销单已正式提交，进入审批流程", "data": {"draft_id": draft_id}}

    # ---------------- ⑤ list_my_records ----------------
    if name == "list_my_records":
        rows = db.list_drafts_by_user(user["user_id"])
        return {"ok": True, "message": f"共 {len(rows)} 条报销记录",
                "data": {"records": rows}}

    # ---------------- ⑥ admin_knowledge_ops：角色硬校验 ----------------
    if name == "admin_knowledge_ops":
        if user.get("role") != "admin":
            return {"ok": False, "code": 1005, "message": "该工具仅管理员可用"}
        action = args.get("action")
        if action == "list":
            docs = [{"id": d["id"], "name": d["original_name"], "chunks": d["chunk_count"]}
                    for d in db.list_docs()]
            return {"ok": True, "message": f"共 {len(docs)} 篇制度文档", "data": {"docs": docs}}
        if action == "delete":
            doc = db.get_doc(args.get("doc_id", ""))
            if doc is None:
                return {"ok": False, "code": 1002, "message": "文档不存在或已删除"}
            db.mark_doc_deleted(doc["id"])
            # 物理文件删除与索引重建由编排层在收到 need_rebuild 后完成
            return {"ok": True, "message": f"文档「{doc['original_name']}」已删除，索引将重建",
                    "data": {"deleted_filename": doc["filename"], "need_rebuild": True}}
        return {"ok": False, "code": 1001, "message": f"未知操作：{action}"}

    return {"ok": False, "code": 1001, "message": f"未知工具：{name}"}
