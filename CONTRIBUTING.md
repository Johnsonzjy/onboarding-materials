# 贡献指南（CONTRIBUTING）

感谢关注本项目！提交 Issue / PR 前请先读完这份简短指南。

## 开发环境

1. Python 3.11+，安装依赖：`pip install -r requirements.txt`
2. 复制 `.env.example` 为 `.env` 并填入密钥（不配置则以 mock 模式离线运行，CI 中即此模式）
3. 启动：`PYTHONPATH=src python -m uvicorn onboarding_assistant.main:app --port 8765`
4. 跑测试：`PYTHONPATH=src python -m pytest tests/test_core.py -v`

## 提交规范

- Commit message 用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：
  `feat: xxx` / `fix: xxx` / `docs: xxx` / `test: xxx` / `chore: xxx`
- 一个 PR 聚焦一件事，附带改动说明与验证方式（截图或 curl 输出）

## 代码约定

- 所有 Python 模块配**详细中文注释**（这是项目既有风格，请保持）
- 错误统一走 `schemas.py` 的错误码 + 中文信息，不裸抛异常给前端
- 涉及报销/工具调用的改动，必须保证 L2 状态机约束不回退（`tests/test_core.py` 会校验）
- 涉及提示词的改动在 `CHANGELOG.md` 的 Unreleased 段记录

## 不接受的内容

- 任何密钥、真实员工数据、真实制度文件（涉密）入库——一律用 mock 数据
- 绕过确认中断（HITL）、放宽校验约束的"便利性"改动

## Issue / PR

- Bug 请附：复现步骤、期望行为、实际行为、服务日志片段
- 使用仓库内置的 Issue 模板与 PR 模板填写
