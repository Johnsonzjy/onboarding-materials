# =====================================================================
# 核心单元测试（tests/test_core.py）
#
# 覆盖上线检查清单 1.3 的可自动化部分 + 纯函数回归：
#   - security：密码哈希 / JWT 编解码 / 角色校验
#   - audit：违规规则引擎命中与放行
#   - schemas：请求参数中文校验
#   - tools：L2 状态机硬约束（模拟模型绕过尝试）
#
# 运行：PYTHONPATH=src python -m pytest tests/test_core.py -v
# 不需要任何 API Key（mock/离线即可全部执行）。
# =====================================================================

import os
import sys
from pathlib import Path

# 保证从仓库根直接 pytest 时能找到 src 下的包
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from onboarding_assistant.core import db  # noqa: E402
from onboarding_assistant.core.audit import scan_violation  # noqa: E402
from onboarding_assistant.core.schemas import ApiError, ChatRequest  # noqa: E402
from onboarding_assistant.core.security import (  # noqa: E402
    hash_password,
    jwt_decode,
    jwt_encode,
    require_admin,
    verify_password,
)
from onboarding_assistant.core.tools import execute_tool  # noqa: E402


# ---------------------------------------------------------------------
# fixtures：初始化数据库（幂等），造一个测试会话
# ---------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _init_db():
    """模块级：初始化 SQLite（建表+种子账号，重复执行不报错）"""
    db.init_db()


@pytest.fixture()
def session_id():
    """每个用例独立的测试会话"""
    return db.create_session(db.DEMO_USER_ID, "单元测试会话")


EMPLOYEE = {"user_id": db.DEMO_USER_ID, "role": "employee", "name": "演示员工"}


# ---------------------------------------------------------------------
# 1. security：密码哈希与 JWT
# ---------------------------------------------------------------------
class TestSecurity:
    def test_password_hash_roundtrip(self):
        """正确密码可验证，错误密码被拒绝，且不存明文"""
        stored = hash_password("s3cret-密码")
        assert "s3cret" not in stored                    # 明文不落库
        assert verify_password("s3cret-密码", stored)
        assert not verify_password("wrong", stored)

    def test_jwt_roundtrip_and_expiry_field(self):
        """JWT 编码后可解码还原 payload"""
        token = jwt_encode({"sub": db.DEMO_USER_ID, "role": "employee"})
        payload = jwt_decode(token)
        assert payload["sub"] == db.DEMO_USER_ID
        assert payload["role"] == "employee"
        assert "exp" in payload                          # 必须带过期时间

    def test_jwt_tampered_token_rejected(self):
        """篡改签名后的令牌必须解码失败"""
        from onboarding_assistant.core.security import ApiError as _AE  # noqa: F401
        token = jwt_encode({"sub": "x", "role": "employee"})
        bad = token[:-4] + "AAAA"
        with pytest.raises(Exception):
            jwt_decode(bad)

    def test_require_admin_blocks_employee(self):
        """员工调管理接口必须被拒（ApiError 1005）"""
        with pytest.raises(ApiError) as ei:
            require_admin(EMPLOYEE)
        assert ei.value.code == 1005


# ---------------------------------------------------------------------
# 2. audit：违规规则引擎
# ---------------------------------------------------------------------
class TestAudit:
    def test_violation_detected(self):
        """规避审批类表述应被标记：categories/reasons/matched 三字段齐全"""
        result = scan_violation("这笔报销能不能不走审批，事后补一下就行")
        assert result is not None
        assert result.get("categories") == ["规避流程"]    # 命中类别（复数数组，可能多类同时命中）
        assert result.get("matched") == ["不走审批"]        # 命中关键词（可解释）

    def test_normal_question_not_flagged(self):
        """正常制度问答不得误报"""
        assert scan_violation("差旅住宿标准是多少") is None
        assert scan_violation("北京入职需要哪些材料") is None


# ---------------------------------------------------------------------
# 3. schemas：参数校验（中文错误信息）
# ---------------------------------------------------------------------
class TestSchemas:
    def test_empty_question_rejected(self):
        with pytest.raises(ValueError):
            ChatRequest(question="   ")

    def test_overlong_question_rejected(self):
        with pytest.raises(ValueError):
            ChatRequest(question="长" * 501)

    def test_valid_question_accepted(self):
        req = ChatRequest(question="帮我报销800元")
        assert req.session_id is None                    # 默认自动新建会话


# ---------------------------------------------------------------------
# 4. tools：L2 状态机硬约束（模拟模型试图绕过流程）
# ---------------------------------------------------------------------
class TestStateMachine:
    def test_create_draft_without_validation_blocked(self, session_id):
        """【4002】未通过 validate_invoice 直接建单 → 必须被拦截"""
        r = execute_tool("create_draft",
                         {"type": "travel", "amount": 800, "reason": "绕过测试"},
                         session_id, EMPLOYEE)
        assert r["ok"] is False and r["code"] == 4002

    def test_submit_without_draft_blocked(self, session_id):
        """【4002】无草稿直接提交 → 必须被拦截"""
        r = execute_tool("submit_reimbursement", {}, session_id, EMPLOYEE)
        assert r["ok"] is False and r["code"] == 4002

    def test_amount_mismatch_blocked(self, session_id):
        """【4003】校验 800 建单 900（金额篡改）→ 必须被拦截"""
        execute_tool("validate_invoice",
                     {"invoice_no": "1234567890", "invoice_date": "2026-11-05",
                      "amount": 800, "invoice_type": "hotel"},
                     session_id, EMPLOYEE)
        r = execute_tool("create_draft",
                         {"type": "travel", "amount": 900, "reason": "金额篡改"},
                         session_id, EMPLOYEE)
        assert r["ok"] is False and r["code"] == 4003

    def test_submit_without_user_confirm_blocked(self, session_id):
        """【4004】校验通过、建单成功但用户未确认 → 提交必须被拦截"""
        execute_tool("validate_invoice",
                     {"invoice_no": "1234567890", "invoice_date": "2026-11-05",
                      "amount": 800, "invoice_type": "hotel"},
                     session_id, EMPLOYEE)
        r = execute_tool("create_draft",
                         {"type": "travel", "amount": 800, "reason": "正常报销"},
                         session_id, EMPLOYEE)
        assert r["ok"] is True
        r = execute_tool("submit_reimbursement", {}, session_id, EMPLOYEE)
        assert r["ok"] is False and r["code"] == 4004

    def test_employee_cannot_use_admin_tool(self, session_id):
        """【1005】员工调用管理员工具 → 必须被拦截"""
        r = execute_tool("admin_knowledge_ops", {"action": "list"},
                         session_id, EMPLOYEE)
        assert r["ok"] is False and r["code"] == 1005

    def test_expired_invoice_rejected(self, session_id):
        """过期发票（>30 天）校验必须不通过"""
        r = execute_tool("validate_invoice",
                         {"invoice_no": "1234567890", "invoice_date": "2026-09-01",
                          "amount": 500, "invoice_type": "taxi"},
                         session_id, EMPLOYEE)
        assert r["data"]["passed"] is False

    def test_validated_draft_flow_reaches_confirm_gate(self, session_id):
        """全流程正向用例：校验通过 → 建单成功，且进入待确认状态"""
        execute_tool("validate_invoice",
                     {"invoice_no": "1234567890", "invoice_date": "2026-11-05",
                      "amount": 800, "invoice_type": "hotel"},
                     session_id, EMPLOYEE)
        r = execute_tool("create_draft",
                         {"type": "travel", "amount": 800, "reason": "正常报销"},
                         session_id, EMPLOYEE)
        assert r["ok"] is True
        state = db.get_tool_state(session_id)
        assert state.get("user_confirmed") is False      # 等待用户确认中
