# =====================================================================
# 接口说明（docs/api.md）
#
# 基础地址：http://127.0.0.1:8765（本地默认）
# 统一响应结构：{ "code": 0, "message": "ok", "data": ... }
# 失败：code 非 0，message 为中文可读信息
# 认证：REST 接口用 Authorization: Bearer <jwt>；SSR 后台页面用 HttpOnly Cookie
# =====================================================================

# 一、错误码总表

| code | 含义 | HTTP 状态 |
|---|---|---|
| 0 | 成功 | 200 |
| 1001 | 参数校验失败（缺字段/格式错误/超长） | 400 |
| 1002 | 会话不存在 / 文档不存在 | 404 |
| 1003 | 知识库未初始化或为空 | 503 |
| 1004 | 未登录 / 令牌无效或过期 | 401 |
| 1005 | 已登录但权限不足（需管理员） | 403 |
| 1006 | 登录失败（用户名或密码错误） | 401 |
| 3001 | LLM 调用失败（含供应商不可达） | SSE error 事件 |
| 4001 | 票据校验未通过 | 工具结果 |
| 4002 | 未完成前置校验（状态机拦截） | 工具结果 |
| 4003 | 建单金额与校验结果不一致 | 工具结果 |
| 4004 | 用户未确认即提交 | 工具结果 |
| 5000 | 系统内部错误 | 500 |

# 二、认证

## POST /api/auth/login
```json
// 请求
{ "username": "admin", "password": "admin123" }
// 响应 data：{ "token": "<jwt>", "role": "admin", "name": "管理员" }
```

## GET /api/auth/me
返回当前登录用户（校验 Bearer 令牌有效性）。

# 三、员工问答（SSE）

## POST /chat
```json
// 请求体（需登录）
{ "session_id": "可选，为空自动新建", "question": "≤500字" }
```
响应 `text/event-stream`，事件时序：

| 事件 | 载荷 | 说明 |
|---|---|---|
| `meta` | `{"session_id": "..."}` | 新建会话时回传 |
| `status` | `{"stage": "retrieving", "message": "正在检索制度…"}` | 阶段提示 |
| `citations` | `[{rank, source, snippet}]` | 检索完成，正文前推送 |
| `tool_call` | `{"name", "label", "status", "result"}` | 智能体工具执行过程 |
| `pending_confirm` | `{"type","type_label","amount","reason","hint"}` | **HITL 中断**：建单前等待确认 |
| `delta` | `{"content": "..."}` | 正文逐字流式 |
| `done` | `{"session_id", "message_id"}` | 结束信号 |
| `error` | `{"code", "message"}` | 流内错误（HTTP 头已发出无法改状态码） |

## GET /sessions/{session_id}/history
回放历史消息（过滤 role=tool），assistant 消息含 citations。

## GET /health
健康检查（无需登录）：服务存活 + 知识库就绪 + LLM 供应商。

# 四、管理接口（★仅管理员★，Bearer 或 Cookie）

## 知识库
| 接口 | 说明 |
|---|---|
| `POST /api/admin/kb/upload` | multipart 上传 .md/.pdf → 切片 → 向量化 → 重建索引 |
| `GET /api/admin/kb/list` | 文档列表（切片数/状态/上传时间） |
| `DELETE /api/admin/kb/{doc_id}` | 删除文档并重建索引 |

## 提示词模板
| 接口 | 说明 |
|---|---|
| `GET /api/admin/prompt` | 当前生效模板 + 版本号 |
| `PUT /api/admin/prompt` | 保存为新版本并激活（问答实时读库，立即生效） |
| `GET /api/admin/prompt/versions` | 版本历史 |
| `POST /api/admin/prompt/versions/{id}/activate` | 回滚到历史版本 |

## 审计与日志
| 接口 | 说明 |
|---|---|
| `GET /api/admin/audit` | 筛选：employee_no / start / end / violation_only / 分页 |
| `GET /api/admin/audit/stats` | 违规统计（仪表盘） |
| `GET /api/admin/rounds?session_id=` | Agent 轮次决策日志（工具名/参数/耗时/结果摘要） |

# 五、工具调用顺序约束（Agent 内部协议）

```
policy_search（可独立） → validate_invoice（必须 passed=true）
  → [HITL 中断：pending_confirm → 用户确认]
  → create_draft（L2 校验金额一致，否则 4003）
  → [用户"确认提交"] → submit_reimbursement
```
任何越序尝试被服务端状态机以 4002/4003/4004 拒绝；管理员工具仅 admin 角色可执行。
