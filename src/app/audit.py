# -*- coding: utf-8 -*-
"""
对话违规扫描模块（批4）
- 需求口径：标记"疑似违规"的问答（不限报销场景，覆盖制度合规的通用风险面）
- 实现方式：关键词规则引擎（不调 LLM，可解释、零成本、毫秒级）
- 规则说明：原型阶段为演示级规则，误报/漏报可通过增删 RULES 调整；
           生产建议升级为"规则初筛 + LLM 复核"两级。
"""
import re

# 规则表：(正则模式, 违规类别, 展示原因)
# 命中任一模式即标记该条消息为疑似违规
RULES = [
    (r"虚报|多报.{0,4}报销|假发票|虚开|代开发票", "虚假报销", "疑似虚假报销表述"),
    (r"不开发票|没有发票.{0,6}(报销|怎么办)|无票报销|不要发票", "规避票据", "疑似规避发票/票据要求"),
    (r"回扣|贿赂|好处费|辛苦费|红包.{0,4}(给|送)", "商业贿赂", "疑似商业贿赂相关表述"),
    (r"绕过审批|跳过审批|不走审批|不走流程|先斩后奏|私下处理|私下解决", "规避流程", "疑似规避公司审批/流程"),
    (r"伪造|假学历|假证明|假证书|假资质|做假证", "伪造材料", "疑似伪造材料/证件"),
    (r"泄露|出卖|卖给对手|卖给竞争对手|外传.{0,4}(资料|数据)|带走.{0,4}(资料|数据)", "信息泄露", "疑似泄露/带走内部信息"),
    (r"偷税|逃税|洗钱|走私|行贿", "违法表述", "疑似涉及违法行为表述"),
    (r"报复|威胁.{0,4}(同事|领导)|打架|斗殴", "职场暴力", "疑似涉及暴力/威胁表述"),
]


def scan_violation(text: str) -> dict | None:
    """扫描一条消息文本，命中规则则返回违规标记结构，否则返回 None。

    返回：{"categories": [...], "reasons": [...], "matched": [...]}
    该结构会被存入 messages.meta.violation，审计页据此筛选与展示。
    """
    if not text:
        return None
    categories, reasons, matched = [], [], []
    for pattern, category, reason in RULES:
        hit = re.search(pattern, text)
        if hit:
            categories.append(category)
            reasons.append(reason)
            matched.append(hit.group(0))
    if not categories:
        return None
    return {"categories": categories, "reasons": reasons, "matched": matched}
