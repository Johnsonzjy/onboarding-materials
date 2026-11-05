# 更新日志（CHANGELOG）

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Planned（规划中）
- PostgreSQL + pgvector 迁移评估（SQLite 仅限试点规模）
- 发票查验 API 对接具体供应商（百望/航信/票易通）真票验证
- 30 条评测用例自动化执行器与质量报告

## [0.5.0] - 2026-11-05

### Added
- Agent Harness 执行循环：模型决策 → 执行工具 → 结果回填 → 再决策，最多 5 轮
- 每轮决策日志表 `agent_round_logs`（工具名/参数/耗时/结果摘要），供后续质量分析
- `create_draft` 调用前强制中断（HITL）：草稿要素流式推送确认卡片，确认后恢复执行，取消则不建单
- 前端确认卡片组件（确认/取消按钮复用审计链路）
- 30 条评测用例 `tests/eval_cases.json`（制度 10 / 工具调用 10 / 违规意图 5 / 多轮上下文 5）
- 上线前检查清单 `docs/上线前检查清单.md`

### Fixed
- Harness 重写后丢失"确认词解锁 submit"的 L3 预检（表现为确认提交被 4004 拦截），已在 run_agent 入口补回

## [0.4.0] - 2026-11-04

### Added
- 报销助手升级为自主决策智能体：6 个 Function Calling 工具（policy_search / validate_invoice / create_draft / submit_reimbursement / list_my_records / admin_knowledge_ops）
- L2 状态机硬约束：校验前置（4002）、金额一致（4003）、用户确认（4004）、角色校验（1005），模型无法绕过
- 发票查验 API 适配器（供应商可配置，无 Key 降级规则校验，失败 fail-closed）
- 前端工具调用过程可视化（工具卡片：执行中转圈 → 成功/失败 + 结果摘要）

### Fixed
- mock 代理绕过统一执行器导致工具调用漏审计，改为全部经 `_run_tool` 落库
- SQLite 加列后 INSERT 列数不匹配，全部改为显式列名根治

## [0.3.0] - 2026-11-04

### Added
- 管理后台（SSR，SaaS 风格）：仪表盘 / 知识库管理 / 提示词模板 / 对话审计
- JWT 登录鉴权：员工/管理员双角色，Bearer（API）+ HttpOnly Cookie（SSR 页面）双通道，pbkdf2 口令哈希
- 知识库管理：上传 md/pdf（pypdf 提取）→ 500 字切片 → 向量化 → 重建索引，支持删除
- 提示词模板：存库多版本管理，问答实时读取，修改即生效，支持回滚
- 对话审计：按员工/时间筛选，违规规则引擎（8 类通用风险）自动打标
- 违规审计原则从"疑似违规报销"放宽为"疑似违规"（不限报销场景）

### Fixed
- 两处 SQL 列数不匹配（sessions/prompt_templates）、漏 import re

## [0.2.0] - 2026-11-03

### Added
- 员工问答入口：`POST /chat` SSE 流式问答（meta/status/citations/delta/done/error 七类事件）
- 会话与消息持久化（SQLite，沿用阶段 1 表结构），每次请求携带最近 6 轮历史
- 聊天前端：fetch + ReadableStream 手工解析 SSE、打字机效果、检索状态条、引用卡片
- 统一错误码与中文错误信息（1001 参数 / 1002 会话 / 1003 知识库 / 3001 LLM / 5000 内部）

### Fixed
- sessions 表 INSERT 列数不匹配、错误响应泄露内部字段

## [0.1.0] - 2026-11-02

### Added
- 阶段 1 最小原型：单文件 RAG 问答脚本（markdown 500 字切片 → FAISS+BM25 混合检索 → LLM 生成，供应商可配置）
- 一页纸需求规格（十问十答定版：公有云多租户 SaaS、规则引擎+工作流+消息总线+集成层）
- 技术架构方案：四层架构 / 目录结构 / 数据库 5 表设计 / 第一周最小验证目标
