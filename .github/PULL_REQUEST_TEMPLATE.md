# Pull Request

## 改动说明
<!-- 做了什么？为什么？关联 Issue：Closes #xxx -->

## 改动类型
- [ ] 新功能（feat）
- [ ] 缺陷修复（fix）
- [ ] 文档（docs）
- [ ] 测试（test）
- [ ] 重构/杂项（refactor/chore）

## 自查清单
- [ ] 核心模块带详细中文注释
- [ ] `PYTHONPATH=src python -m pytest tests/test_core.py -v` 通过
- [ ] 未改变 L2 状态机约束（4002/4003/4004/1005 行为不变，除非 Issue 明确要求）
- [ ] HITL 确认中断行为未被绕过
- [ ] 未提交任何密钥 / 真实数据 / 运行时产物（chat.db、日志）
- [ ] CHANGELOG.md 的 Unreleased 段已更新

## 验证方式
<!-- 如何验证本次改动（curl 命令 / 操作步骤 / 截图） -->
