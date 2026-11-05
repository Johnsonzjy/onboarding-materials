# 跨区域入职材料一次告知 · 制度智能问答助手（onboarding-materials）

面向企业**员工**与**管理员**的智能问答系统：员工用自然语言查询跨区域入职材料与报销制度（RAG 混合检索、答案带引用来源），并以对话方式发起报销（Agent 自主调用工具 + 关键节点人工确认）；管理员维护制度知识库、提示词模板与对话审计。

## 核心特性

- **制度问答**：BM25 + 向量（FAISS）混合检索 Top3，回答末尾附引用来源；制度中没有的内容明确回答"制度中未找到"，不编造
- **报销智能体**：6 个 Function Calling 工具 + Harness 执行循环（≤5 轮）；L2 状态机硬约束——未通过发票校验绝不允许建单，金额篡改/未确认提交均被服务端拦截
- **人机协同（HITL）**：创建报销草稿前强制中断，草稿要素以确认卡片推送，用户确认/取消后恢复
- **管理后台**（SSR，SaaS 风格）：JWT 登录鉴权（员工/管理员双角色）、知识库上传/删除（md/pdf）、提示词模板多版本实时生效、对话审计与疑似违规标记、Agent 轮次决策日志
- **供应商可替换**：LLM / Embedding / 发票查验 API 均通过环境变量切换，内置 OpenAI 兼容协议适配与离线 mock 模式

## 快速开始

```bash
# 1. 安装依赖（Python 3.11+）
pip install -r requirements.txt

# 2. 配置密钥（可选，不配置则以 mock 模式离线运行）
cp .env.example .env    # 填入 LLM_API_KEY / EMBED_API_KEY / INVOICE_API_* 等

# 3. 启动服务（仓库根目录执行）
#    Windows PowerShell:  $env:PYTHONPATH="src"; python -m uvicorn onboarding_assistant.main:app --port 8765
#    Linux / macOS bash:  PYTHONPATH=src python -m uvicorn onboarding_assistant.main:app --port 8765
#    或直接用脚本:        bash scripts/deploy.sh

# 4. 访问
#    员工聊天页   http://127.0.0.1:8765/
#    登录页       http://127.0.0.1:8765/login
#    管理后台     http://127.0.0.1:8765/admin
```

**内置演示账号**（生产环境必须修改/禁用）：

| 角色 | 用户名 | 密码 | 入口 |
|---|---|---|---|
| 管理员 | `admin` | `admin123` | `/admin` |
| 员工 | `demo001` | `123456` | `/` |

**体验主线**：员工登录后输入"北京入职需要哪些额外材料"（看引用来源），再输入"帮我报销800元的差旅住宿费，发票号1234567890"（看工具调用过程 → 确认卡片 → 建单 → 确认提交）。

## 项目结构

```
onboarding-materials/
├── README.md            # 本文件：项目是啥、怎么装、怎么跑
├── LICENSE              # MIT 许可证
├── CHANGELOG.md         # 版本更新记录
├── CONTRIBUTING.md      # 贡献指南
├── requirements.txt     # Python 依赖清单
├── .env.example         # 环境变量模板（密钥不回家）
│
├── src/                 # 源代码主目录
│   └── onboarding_assistant/    # 主包
│       ├── main.py      # FastAPI 入口：路由/生命周期/异常处理
│       ├── core/        # 核心业务逻辑
│       │   ├── agent.py     # Harness 执行循环（决策→执行→回填→再决策，HITL 中断）
│       │   ├── tools.py     # 6 个工具 + L2 状态机硬约束 + 发票查验 API 适配器
│       │   ├── chat.py      # SSE 问答编排
│       │   ├── rag.py       # 混合检索（FAISS+BM25）/切片/LLM 流式封装
│       │   ├── db.py        # SQLite 数据访问（会话/消息/文档/草稿/轮次日志）
│       │   ├── security.py  # JWT + pbkdf2 密码 + 角色校验
│       │   ├── audit.py     # 违规规则引擎（8 类通用风险）
│       │   └── schemas.py   # Pydantic 模型 + 统一错误码
│       ├── static/      # 员工聊天前端（原生 JS，SSE 解析/打字机/工具卡片）
│       └── templates/   # 管理后台 Jinja2 模板
│
├── docs/                # 文档
│   ├── design.md               # 技术架构方案（四层架构/目录/表设计）
│   ├── api.md                  # 接口说明（REST + SSE 事件协议）
│   ├── requirements-onepager.md  # 一页纸需求规格
│   └── 上线前检查清单.md          # 上线评审 8 大项 40+ 检查点
│
├── tests/               # 测试
│   ├── eval_cases.json  # 30 条评测用例（制度/工具/违规/多轮）
│   └── test_core.py     # 核心单元测试（安全/审计/参数校验）
│
├── examples/            # 示例
│   └── demo_rag_cli.py  # 阶段1 单文件 RAG 命令行 demo（无 Web 依赖）
│
├── scripts/             # 构建/部署脚本
│   ├── build.sh         # 依赖固化（pip freeze）
│   └── deploy.sh        # 启动服务
│
├── config/              # 非机密配置（默认参数参考）
│   └── default.yaml
│
├── data/                # 运行时数据（chat.db 自动生成，不入库）+ 知识库样例
│   └── knowledge/       # 制度 markdown 文档（管理后台上传也落在这里）
│
└── .github/             # GitHub 专用（CI/CD、Issue/PR 模板）
    └── workflows/ci.yml
```

## 文档导航

| 想了解 | 看这里 |
|---|---|
| 架构设计、数据库表、目录演进 | [docs/design.md](docs/design.md) |
| 全部 REST 接口与 SSE 事件协议 | [docs/api.md](docs/api.md) |
| 需求边界与角色定义 | [docs/requirements-onepager.md](docs/requirements-onepager.md) |
| 上线前评审项 | [docs/上线前检查清单.md](docs/上线前检查清单.md) |
| 版本历史 | [CHANGELOG.md](CHANGELOG.md) |

## 许可证

[MIT](LICENSE)
