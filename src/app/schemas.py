# -*- coding: utf-8 -*-
"""
统一响应与错误码定义（所有接口遵守）
- 所有 JSON 接口返回三段式：{code, message, data}
- 错误码分段：1xxx=参数/会话，2xxx=知识库，3xxx=模型调用，5xxx=系统内部
"""
from pydantic import BaseModel, field_validator

# ---------------- 错误码常量 ----------------
CODE_OK = 0                 # 成功
CODE_PARAM = 1001           # 参数校验失败
CODE_SESSION_NOT_FOUND = 1002  # 会话不存在
CODE_KB_NOT_READY = 1003    # 知识库未初始化或为空
CODE_LLM = 3001             # LLM 调用失败
CODE_INTERNAL = 5000        # 系统内部错误
CODE_UNAUTHORIZED = 1004    # 未登录 / 令牌无效或过期
CODE_FORBIDDEN = 1005       # 已登录但权限不足（需管理员）
CODE_AUTH_FAILED = 1006     # 登录失败（用户名或密码错误）
CODE_INVOICE_FAILED = 4001  # 票据校验未通过
CODE_NO_PREREQ = 4002       # 未完成前置校验（如未 validate_invoice 就 create_draft）
CODE_AMOUNT_MISMATCH = 4003 # 报销金额与校验通过的发票金额不一致
CODE_NOT_CONFIRMED = 4004   # 用户未确认草稿即请求提交


def ok(data=None, message: str = "ok") -> dict:
    """成功响应的统一结构"""
    return {"code": CODE_OK, "message": message, "data": data}


def fail(code: int, message: str, http_status: int = 400) -> dict:
    """失败响应的统一结构（_http_status 供异常处理器读取，返回前端前会被剥离）"""
    return {"code": code, "message": message, "data": None, "_http_status": http_status}


def public_body(body: dict) -> dict:
    """剥掉下划线开头的内部字段（如 _http_status），只返回对前端的干净结构"""
    return {k: v for k, v in body.items() if not k.startswith("_")}


class ApiError(Exception):
    """业务异常：路由中抛出后，由 main.py 的全局异常处理器转成统一 JSON 结构"""

    def __init__(self, code: int, message: str, http_status: int = 400):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


class ChatRequest(BaseModel):
    """POST /chat 请求体"""
    session_id: str | None = None   # 为空则自动新建会话（设计决策 1）
    question: str                   # 员工的问题

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        """中文校验：非空且不超过 500 字，失败信息直接可读"""
        v = (v or "").strip()
        if not v:
            raise ValueError("问题内容不能为空")
        if len(v) > 500:
            raise ValueError("问题长度不能超过 500 字")
        return v


class LoginRequest(BaseModel):
    """POST /api/auth/login 请求体"""
    username: str
    password: str

    @field_validator("username", "password")
    @classmethod
    def not_empty(cls, v: str, info) -> str:
        """登录字段中文校验"""
        v = (v or "").strip()
        if not v:
            raise ValueError(f"{info.field_name} 不能为空")
        return v
