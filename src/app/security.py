# -*- coding: utf-8 -*-
"""
认证与权限核心模块（批1）
- 手写 HS256 JWT（不引入 PyJWT，原型阶段可控可读；生产可平替为 PyJWT）
- pbkdf2 口令哈希（存储格式：salt$hash，永不存明文）
- FastAPI 依赖：get_current_user（登录校验）/ require_admin（管理员校验）
- 令牌获取顺序：Authorization: Bearer 头（API 用）→ Cookie（服务端渲染页面用）
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import Request

from app.schemas import ApiError

# 签名密钥：默认开发密钥，生产环境必须用环境变量覆盖
JWT_SECRET = os.getenv("JWT_SECRET", "onboarding-dev-secret-change-me")
TOKEN_TTL_SECONDS = 12 * 3600   # 令牌有效期 12 小时


# =====================================================================
# JWT：base64url(header) . base64url(payload) . HMAC-SHA256 签名
# =====================================================================

def _b64e(raw: bytes) -> bytes:
    """字节 → urlsafe base64（去掉填充符 =）"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _b64d(text: str) -> bytes:
    """urlsafe base64 → 字节（补回填充符）"""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwt_encode(payload: dict, expires_in: int = TOKEN_TTL_SECONDS) -> str:
    """签发 JWT：自动附加 iat（签发时间）与 exp（过期时间）"""
    header = {"alg": "HS256", "typ": "JWT"}
    body = {**payload,
            "iat": int(time.time()),
            "exp": int(time.time()) + expires_in}
    h = _b64e(json.dumps(header, separators=(",", ":")).encode())
    p = _b64e(json.dumps(body, separators=(",", ":")).encode())
    # 签名 = HMAC-SHA256(密钥, header.payload)
    sig = hmac.new(JWT_SECRET.encode(), h + b"." + p, hashlib.sha256).digest()
    return f"{h.decode()}.{p.decode()}.{_b64e(sig).decode()}"


def jwt_decode(token: str) -> dict:
    """校验并解析 JWT；任何失败抛 ValueError（上层转 1004 未授权）"""
    try:
        h, p, s = token.split(".")
        # 1. 验签：constant_time 比较防时序攻击
        expect = hmac.new(JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, _b64d(s)):
            raise ValueError("令牌签名不匹配")
        # 2. 验期
        body = json.loads(_b64d(p))
        if body.get("exp", 0) < time.time():
            raise ValueError("登录已过期，请重新登录")
        return body
    except ValueError:
        raise
    except Exception as e:   # noqa: BLE001 —— 格式损坏等统一归为令牌无效
        raise ValueError("令牌无效") from e


# =====================================================================
# 口令哈希：pbkdf2_hmac-SHA256，10 万轮迭代
# =====================================================================

def hash_password(password: str) -> str:
    """生成 '盐$哈希' 存储串；盐为随机 16 字节，每次不同"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令：按存储串中的盐重算哈希后常数时间比较"""
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                     bytes.fromhex(salt_hex), 100_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:   # noqa: BLE001 —— 存储串格式异常一律判定失败
        return False


# =====================================================================
# FastAPI 依赖：接口级权限校验
# =====================================================================

def get_current_user(request: Request) -> dict:
    """登录校验依赖：任何受保护接口挂上它即要求登录。

    令牌来源：Authorization: Bearer 头优先（前端 fetch 用），其次 Cookie
    access_token（服务端渲染页面/同源导航用）。
    """
    token = None
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        raise ApiError(1004, "未登录，请先登录", 401)
    try:
        payload = jwt_decode(token)
    except ValueError as e:
        raise ApiError(1004, str(e), 401) from e
    return {"user_id": payload.get("sub"),
            "role": payload.get("role"),
            "name": payload.get("name"),
            "username": payload.get("username")}


def require_admin(user: dict) -> dict:
    """管理员校验依赖：先过 get_current_user，再校验角色，返回用户信息"""
    if user.get("role") != "admin":
        raise ApiError(1005, "该操作需要管理员权限", 403)
    return user


def admin_from_request(request: Request) -> dict | None:
    """供服务端渲染页面使用：从 Cookie 校验管理员身份。

    与接口依赖不同：失败不抛异常而是返回 None，由页面路由重定向到 /login。
    """
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = jwt_decode(token)
    except ValueError:
        return None
    if payload.get("role") != "admin":
        return None
    return {"user_id": payload.get("sub"), "name": payload.get("name")}
