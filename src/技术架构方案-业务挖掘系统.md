# 技术架构方案 — 跨区域入职材料一次告知 · 业务挖掘（V0.1）

> 角色：资深 Python 后端架构师｜约束：FastAPI（异步）/ Python 3.11+ / LLM 走 API 可替换供应商 / 知识库基于上一套系统建立
> 角色定义：普通员工（问答 + 发起报销）、管理员（维护制度知识库 + 看记录）
> 编制日期：2026-10-30

---

## 1. 四层架构图（文字版）

```
┌─────────────────────────────────────────────────────────────┐
│ 入口层 Entry Layer                                            │
│  · 多端接入：Web / 移动App / 企业微信(预留)                    │
│  · 协议：REST(JSON) + WebSocket/SSE(流式问答)                 │
│  · 认证鉴权：SSO(对接统一认证) + JWT + RBAC(employee/admin)   │
│  · 限流 / 审计 / 租户路由(多法人隔离)                         │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 服务层 Service Layer（无状态领域服务）                         │
│  · SessionService   会话生命周期                             │
│  · ChatService      问答编排入口(调编排层)                    │
│  · ReimbursementService  报销草稿生成/提交/查询               │
│  · KnowledgeService 制度文档 CRUD/导入/切片/发布              │
│  · RecordService    管理员看板(会话+报销记录)                │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 智能体编排层 Agent Orchestration Layer（核心智能）            │
│  · LLMProvider 抽象(可替换: OpenAI/通义/DeepSeek/自建)        │
│  · Context Builder(注入 角色+租户+区域+用户画像)              │
│  · Retriever  RAG(向量召回 + 重排 + 元数据过滤)               │
│  · Tool Registry & Executor                                  │
│       - policy_search(查制度)                                │
│       - create_reimbursement_draft(发起报销)                 │
│       - list_records(查记录,仅管理员)                        │
│  · Memory  短期会话记忆 + 长期用户/区域画像                   │
│  · Orchestrator  ReAct/Plan 循环 + Guardrails(安全护栏)       │
│  · Streaming 输出(SSE 逐字推送)                              │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ 数据模型层 Data Model Layer                                   │
│  · PostgreSQL : users / sessions / messages /                │
│                  policy_docs / reimbursement_drafts           │
│  · 向量库 pgvector : 文档切片 embedding + 元数据(region/tenant)│
│  · 对象存储 : 原始制度文档 / 报销附件                         │
│  · Redis : 会话上下文缓存 / 限流 / 流式缓冲                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 推荐目录结构（tree）

```
onboarding-agent/
├── pyproject.toml
├── README.md
├── .env.example
├── alembic/                      # 数据库版本迁移
│   └── versions/
├── app/
│   ├── main.py                   # FastAPI 入口 + 生命周期
│   ├── core/
│   │   ├── config.py             # 配置(LLM供应商/DB/向量库)
│   │   ├── security.py           # JWT / RBAC / SSO 适配
│   │   └── logging.py
│   ├── api/
│   │   ├── deps.py               # 依赖注入(当前用户/租户)
│   │   └── v1/
│   │       ├── router.py
│   │       ├── auth.py
│   │       ├── chat.py           # 员工问答(流式)
│   │       ├── reimbursement.py  # 报销发起/草稿
│   │       └── admin.py          # 知识库维护 + 记录查看
│   ├── services/                 # 领域服务(调编排层)
│   │   ├── session_service.py
│   │   ├── chat_service.py
│   │   ├── reimbursement_service.py
│   │   └── knowledge_service.py
│   ├── agent/                    # 智能体核心
│   │   ├── orchestrator.py
│   │   ├── llm/
│   │   │   ├── base.py           # LLMProvider 抽象接口
│   │   │   └── openai_provider.py / deepseek_provider.py ...
│   │   ├── retriever.py          # RAG 召回+重排
│   │   ├── tools/                # 工具集
│   │   │   ├── base.py
│   │   │   ├── policy_search.py
│   │   │   ├── reimbursement_tool.py
│   │   │   └── registry.py
│   │   ├── memory.py
│   │   └── prompts.py
│   ├── models/                   # SQLAlchemy ORM
│   │   ├── user.py / session.py / message.py
│   │   ├── policy_doc.py / reimbursement.py
│   ├── schemas/                  # Pydantic 出入参
│   ├── repositories/            # 数据访问
│   ├── integrations/
│   │   ├── vector_store.py       # pgvector 封装
│   │   └── object_storage.py
│   └── tasks/                    # 后台任务(异步切片/向量化)
├── tests/
└── scripts/                     # 知识库导入脚本(读上一套系统产出)
```

---

## 3. 数据库表设计（PostgreSQL）

### users（用户）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID PK | |
| employee_no | VARCHAR UNIQUE | 工号 |
| name | VARCHAR | |
| role | ENUM(employee, admin) | 角色 |
| org_id | VARCHAR | 租户/法人(隔离) |
| region_code | VARCHAR | 归属区域(影响政策召回) |
| sso_subject | VARCHAR | 外部认证标识 |
| status | ENUM(active, disabled) | |
| created_at / updated_at | TIMESTAMPTZ | |

### sessions（会话）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID PK | |
| user_id | FK→users | |
| tenant_id | VARCHAR | 租户 |
| title | VARCHAR | 首条消息自动生成 |
| channel | ENUM(web, app, wecom) | |
| status | ENUM(active, closed) | |
| created_at / last_active_at | TIMESTAMPTZ | |

### messages（消息）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID PK | |
| session_id | FK→sessions | |
| role | ENUM(user, assistant, system, tool) | |
| content | TEXT | |
| meta | JSONB | 引用(citations)/工具调用/延迟/Token |
| sequence | INT | 会话内顺序 |
| created_at | TIMESTAMPTZ | |

### policy_docs（报销/制度文档 · 知识库）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID PK | |
| tenant_id | VARCHAR | 租户 |
| title | VARCHAR | |
| category | ENUM(onboarding, travel, entertainment, material…) | 分类 |
| region_code | VARCHAR NULL | 适用区域(NULL=全局) |
| content | TEXT | 正文(markdown) |
| source_ref | VARCHAR NULL | 原始文件/上一套系统来源 |
| version | INT | |
| status | ENUM(draft, published) | |
| created_by | FK→users(admin) | |
| effective_from / effective_to | DATE NULL | 有效期 |
| created_at / updated_at | TIMESTAMPTZ | |

> 切片与向量存于 pgvector（按 doc_id + region/tenant 元数据过滤），不在关系表展开。

### reimbursement_drafts（报销单草稿）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID PK | |
| session_id | FK→sessions NULL | 由哪次对话生成 |
| user_id | FK→users | |
| tenant_id | VARCHAR | |
| type | ENUM(travel, entertainment, material…) | 报销类型 |
| status | ENUM(draft, submitted) | 草稿/已提交 |
| amount | NUMERIC(12,2) | 金额 |
| currency | VARCHAR | 币种 |
| reason | TEXT | 事由 |
| items | JSONB | 明细行 |
| attachments | JSONB | 附件引用(对象存储) |
| llm_summary | TEXT | Agent 提取摘要 |
| created_at / updated_at | TIMESTAMPTZ | |
| submitted_at | TIMESTAMPTZ NULL | |

> 管理员"看记录" = sessions + messages + reimbursement_drafts 联合查询视图，无需额外表。

---

## 4. 第一周最小验证目标

**验收主线**：一条真实对话 → 问政策(带引用) → 发起报销草稿 → 管理员看到记录。

- **Day1–2 脚手架**：FastAPI 项目 + PostgreSQL + pgvector；本地 JWT 鉴权（SSO 先 stub）；employee/admin 两角色路由与权限区分。
- **Day3 知识库闭环**：写 scripts 导入 3–5 篇制度文档（含上一套系统的入职材料规则 + 报销制度）→ 切片 → 向量化入 pgvector。
- **Day4 问答链路**：员工发问 → Retriever 召回(按 tenant+region 过滤) → LLM 生成带引用的回答，SSE 流式返回。
- **Day5 报销 + 管理员**：员工说"帮我发起XX报销" → Agent 调 create_reimbursement_draft 工具 → 草稿入库 → 员工确认；管理员上传/维护 1 篇制度文档，并查看会话与报销记录列表。
- **供应商可替换验证**：LLM 通过配置切换（至少 1 个真实可调用 + 1 个 mock），证明 LLMProvider 抽象生效。

**第一周末交付物**：本地能 uvicorn 起服务，用 curl/简单前端走完上述主线；架构、目录、表结构冻结，作为第二周扩展（多工具、重排、审计、企微接入）的基线。
