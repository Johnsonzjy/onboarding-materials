#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 阶段 1 最小原型：制度知识库问答（RAG · 单文件 · 纯 CLI，无 Web）
=====================================================================
 功能：
   1) 读取本地制度 markdown 文件 → 按 500 字切片 → 向量化 → 存入 FAISS
   2) 命令行输入问题 → BM25 + 向量混合检索 Top3 → 拼入提示词 → 调 LLM 生成回答
   3) LLM 调用封装为独立函数，供应商通过配置/环境变量切换

 依赖安装：
   pip install faiss-cpu numpy rank_bm25 jieba openai

 运行方式：
   python phase1_rag_prototype.py [制度文件.md]
   # 可选环境变量（不设则走 mock 离线模式，保证零成本先跑通流程）：
   #   LLM_PROVIDER=openai|deepseek|qwen|glm|mock
   #   LLM_API_KEY=sk-xxx
   #   EMBED_API_KEY=sk-xxx          （不设则用本地降级向量，仅用于离线演示）
   #   LLM_MODEL / LLM_BASE_URL / EMBED_MODEL / EMBED_BASE_URL 可覆盖默认值

 ─────────────────────────────────────────────────────────────────────
 【复用标注说明】后续阶段（阶段2 Web 化、阶段3 多工具智能体）会直接复用
 的"核心模块"在代码中用  ★核心模块★  标出；仅本阶段使用的胶水代码用
   ◇阶段胶水◇  标出。
=====================================================================
"""

import os
import re
import sys
import hashlib
from typing import List, Tuple

import numpy as np
import faiss                      # Facebook 向量检索库（CPU 版即可）
import jieba                      # 中文分词，BM25 需要
from rank_bm25 import BM25Okapi   # 轻量 BM25 实现


# =====================================================================
# 第 0 部分：配置中心
# ◇阶段胶水◇（阶段 2 会迁移到 app/core/config.py，改为 pydantic-settings）
# =====================================================================

# 不同供应商的默认接入点（均为 OpenAI 兼容协议，一个 SDK 通吃）
PROVIDER_PRESETS = {
    "openai":   {"base_url": "https://api.openai.com/v1",                "model": "gpt-4o-mini"},
    "deepseek": {"base_url": "https://api.deepseek.com",                 "model": "deepseek-chat"},
    "qwen":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "glm":      {"base_url": "https://open.bigmodel.cn/api/paas/v4",     "model": "glm-4-flash"},
}

CONFIG = {
    # ---- LLM 对话模型 ----
    "llm_provider": os.getenv("LLM_PROVIDER", "mock"),          # 供应商，默认 mock（离线）
    "llm_api_key":  os.getenv("LLM_API_KEY", ""),
    "llm_model":    os.getenv("LLM_MODEL", ""),                  # 留空则用供应商默认
    "llm_base_url": os.getenv("LLM_BASE_URL", ""),               # 留空则用供应商默认

    # ---- Embedding 向量模型（RAG 用）----
    "embed_api_key":  os.getenv("EMBED_API_KEY", ""),
    "embed_model":    os.getenv("EMBED_MODEL", "text-embedding-3-small"),
    "embed_base_url": os.getenv("EMBED_BASE_URL", "https://api.openai.com/v1"),

    # ---- RAG 参数 ----
    "chunk_size": 500,        # 每片最大字符数（需求：500 字切片）
    "chunk_overlap": 50,      # 相邻切片重叠字符数，防止语义被切断
    "top_k": 3,               # 混合检索返回条数（需求：Top3）
    "bm25_weight": 0.5,       # 混合打分权重：BM25 占比（向量占 1-该值）
    "local_embed_dim": 256,   # 本地降级向量的维度（仅离线演示用）
}


# =====================================================================
# 第 1 部分：LLM 调用封装（供应商可配置）
# ★核心模块★ —— 阶段 2 将原样迁移到 app/agent/llm/base.py，
#                阶段 3 的 Orchestrator 只依赖本函数，不感知具体供应商。
# =====================================================================

def _build_llm_client():
    """根据配置构造 OpenAI 兼容客户端（DeepSeek/通义/GLM 均兼容此协议）。

    返回 (client, model_name)；mock 模式返回 (None, "mock")。
    """
    provider = CONFIG["llm_provider"]

    # mock 模式：不连任何外部服务，用于零成本验证整条 RAG 流程
    if provider == "mock":
        return None, "mock"

    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise ValueError(f"未知供应商: {provider}，可选: {list(PROVIDER_PRESETS)} 或 mock")

    # 延迟导入：mock 模式下即使没装 openai 也能跑
    from openai import OpenAI
    client = OpenAI(
        api_key=CONFIG["llm_api_key"] or "EMPTY",   # 部分中转站允许占位 key
        base_url=CONFIG["llm_base_url"] or preset["base_url"],
    )
    model = CONFIG["llm_model"] or preset["model"]
    return client, model


def chat_llm(user_prompt: str, system_prompt: str = "你是一个严谨的企业制度问答助手。") -> str:
    """★核心模块★ 统一的 LLM 对话入口（供应商无关）。

    阶段 2/3 的所有"生成"动作（问答、摘要、报销信息抽取）都只调用本函数，
    切换供应商 = 改一个环境变量，业务代码零改动。
    """
    provider = CONFIG["llm_provider"]

    # ---- mock 实现：抽取式"伪回答"，把检索到的片段拼出来，验证链路用 ----
    if provider == "mock":
        # 从提示词中抠出"参考片段"部分，做简单抽取式回答
        m = re.search(r"参考片段：(.*?)\n\n", user_prompt, re.S)
        ctx = m.group(1) if m else "（无检索结果）"
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip().startswith("[")]
        digest = "\n".join(lines[:2])[:300]
        return (
            "【MOCK 模式回答·仅验证链路】根据制度知识库检索结果：\n"
            f"{digest}\n"
            "（配置 LLM_PROVIDER 与 LLM_API_KEY 后可获得真实生成回答）"
        )

    # ---- 真实供应商实现：OpenAI 兼容 chat/completions ----
    client, model = _build_llm_client()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.3,     # 制度问答要稳，温度调低
    )
    return resp.choices[0].message.content


# =====================================================================
# 第 2 部分：Embedding 向量化封装
# ★核心模块★ —— 迁移目标 app/agent/retriever.py（阶段 2 加重排、缓存）
# =====================================================================

def _local_embed(texts: List[str]) -> np.ndarray:
    """本地降级向量：字符 n-gram 哈希法（无需网络/密钥）。

    ⚠️ 仅用于离线演示跑通流程，语义能力弱；生产必须换成 API/本地模型向量。
    """
    dim = CONFIG["local_embed_dim"]
    vecs = np.zeros((len(texts), dim), dtype="float32")
    for i, t in enumerate(texts):
        # 一元 + 二元字符特征，比纯单字更能捕捉局部语义
        feats = [t[j:j + n] for n in (1, 2) for j in range(len(t) - n + 1)]
        for f in feats:
            # 哈希到固定维度桶，带正负号减少碰撞影响（类似 hashing trick）
            h = int(hashlib.md5(f.encode("utf-8")).hexdigest(), 16)
            vecs[i, h % dim] += 1 if (h >> 128) % 2 == 0 else -1
    # L2 归一化，使内积 = 余弦相似度
    norm = np.linalg.norm(vecs, axis=1, keepdims=True)
    norm[norm == 0] = 1
    return (vecs / norm).astype("float32")


def embed_texts(texts: List[str]) -> np.ndarray:
    """★核心模块★ 文本 → 向量矩阵（已 L2 归一化）。

    优先走 Embedding API（OpenAI 兼容）；未配置密钥时自动降级为本地向量。
    """
    if not CONFIG["embed_api_key"]:
        print("[提示] 未配置 EMBED_API_KEY，使用本地降级向量（仅演示用）")
        return _local_embed(texts)

    from openai import OpenAI
    client = OpenAI(api_key=CONFIG["embed_api_key"], base_url=CONFIG["embed_base_url"])
    all_vecs = []
    # 分批请求，避免超出单次请求条数限制
    for i in range(0, len(texts), 16):
        batch = texts[i:i + 16]
        resp = client.embeddings.create(model=CONFIG["embed_model"], input=batch)
        all_vecs.extend([d.embedding for d in resp.data])
    vecs = np.array(all_vecs, dtype="float32")
    norm = np.linalg.norm(vecs, axis=1, keepdims=True)
    return (vecs / norm).astype("float32")


# =====================================================================
# 第 3 部分：文档加载与 500 字切片
# ★核心模块★ —— 迁移目标 app/services/knowledge_service.py + app/tasks/
# =====================================================================

def load_markdown(path: str) -> str:
    """读取 markdown 制度文件为纯文本（md 语法保留，切片时一起向量化即可）。"""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def split_to_chunks(text: str, max_len: int = None, overlap: int = None) -> List[str]:
    """★核心模块★ 500 字滑动窗口切片（带 overlap）。

    策略：优先按空行分段落 → 段落能塞进 500 字就合并；
         超长段落再按 max_len 滑窗硬切，窗口间重叠 overlap 字。
    """
    max_len = max_len or CONFIG["chunk_size"]
    overlap = overlap if overlap is not None else CONFIG["chunk_overlap"]

    # 按空行切段，保留制度文档天然的结构边界
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paragraphs:
        if len(buf) + len(p) + 1 <= max_len:          # 还装得下 → 合并
            buf = f"{buf}\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)                     # 收掉已满的缓冲
            if len(p) <= max_len:
                buf = p                                # 新段落本身就是一片
            else:                                      # 超长段落 → 滑窗硬切
                start = 0
                while start < len(p):
                    chunks.append(p[start:start + max_len])
                    start += max_len - overlap         # 带重叠步进
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


# =====================================================================
# 第 4 部分：FAISS 向量索引 + BM25 索引构建
# ★核心模块★ —— 迁移目标 app/integrations/vector_store.py
# =====================================================================

def build_faiss_index(chunks: List[str]) -> Tuple[faiss.Index, np.ndarray]:
    """向量化全部切片并建立 FAISS 内积索引（向量已归一化 → 内积即余弦）。"""
    vecs = embed_texts(chunks)
    dim = vecs.shape[1]
    index = faiss.IndexFlatIP(dim)   # 内积索引：精确检索，小知识库够用
    index.add(vecs)
    return index, vecs


def _tokenize(text: str) -> List[str]:
    """中文分词（BM25 与查询共用同一分词器，保证词表一致）。"""
    return [w for w in jieba.lcut(text) if w.strip()]


def build_bm25_index(chunks: List[str]) -> BM25Okapi:
    """对全部切片建 BM25 索引（关键词/字面匹配强项，补向量语义之短）。"""
    corpus = [_tokenize(c) for c in chunks]
    return BM25Okapi(corpus)


# =====================================================================
# 第 5 部分：BM25 + 向量 混合检索 Top3
# ★核心模块★ —— 迁移目标 app/agent/retriever.py（阶段 2 加 rerank）
# =====================================================================

def _minmax(arr: np.ndarray) -> np.ndarray:
    """min-max 归一化到 [0,1]，使两路分数可加权融合。"""
    lo, hi = arr.min(), arr.max()
    return (arr - lo) / (hi - lo) if hi > lo else np.zeros_like(arr)


def hybrid_search(question: str, chunks, faiss_index, bm25_index, top_k=None) -> List[dict]:
    """★核心模块★ 混合检索：BM25(关键词) + 向量(语义) 加权融合。

    流程：两路分别对全量切片打分 → 各自 min-max 归一化 → 加权求和 → 取 TopK。
    返回：[{rank, score, text}] 供提示词拼接与引用展示。
    """
    top_k = top_k or CONFIG["top_k"]
    n = len(chunks)

    # ---- 路 1：向量语义召回（FAISS 内积 = 余弦相似度）----
    q_vec = embed_texts([question])                      # (1, dim)
    scores_vec = np.full(n, -1.0, dtype="float32")
    D, I = faiss_index.search(q_vec, n)                  # 全量召回后融合
    for s, idx in zip(D[0], I[0]):
        if idx >= 0:
            scores_vec[idx] = s

    # ---- 路 2：BM25 关键词召回 ----
    scores_bm25 = np.array(bm25_index.get_scores(_tokenize(question)), dtype="float32")

    # ---- 融合：加权求和（权重可调，阶段 2 可换成 RRF/学习排序）----
    fused = (CONFIG["bm25_weight"] * _minmax(scores_bm25)
             + (1 - CONFIG["bm25_weight"]) * _minmax(scores_vec))
    top_idx = np.argsort(fused)[::-1][:top_k]

    return [{"rank": r + 1, "score": float(fused[i]), "text": chunks[i]}
            for r, i in enumerate(top_idx)]


# =====================================================================
# 第 6 部分：提示词拼装
# ★核心模块★ —— 迁移目标 app/agent/prompts.py（模板化 + 引用校验）
# =====================================================================

def build_prompt(question: str, hits: List[dict]) -> str:
    """把检索到的 Top3 片段 + 用户问题拼成最终提示词（带引用编号）。"""
    ctx = "\n\n".join(
        f"[{h['rank']}] {h['text']}" for h in hits
    )
    return (
        "请严格依据以下制度知识库片段回答用户问题。\n"
        "要求：\n"
        "1. 回答末尾用 [编号] 标注引用了哪些片段；\n"
        "2. 片段中没有的信息不要编造，明确说明'现行制度中未查到'；\n"
        "3. 涉及金额/流程的，按片段原文表述。\n\n"
        f"参考片段：\n{ctx}\n\n"
        f"用户问题：{question}"
    )


# =====================================================================
# 第 7 部分：主流程 + 命令行交互
# ◇阶段胶水◇ —— 阶段 2 由 FastAPI 接口替代，核心逻辑已全部在上方模块中
# =====================================================================

def main():
    # ---- 1. 定位制度文件（默认用同目录示例文件）----
    md_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sample_policy.md")
    if not os.path.exists(md_path):
        print(f"❌ 找不到制度文件: {md_path}")
        sys.exit(1)

    # ---- 2. 离线构建知识库：读取 → 切片 → 双索引 ----
    print(f"📖 读取制度文件: {md_path}")
    text = load_markdown(md_path)
    chunks = split_to_chunks(text)
    print(f"✂️  切片完成: {len(chunks)} 片（每片≤{CONFIG['chunk_size']}字, 重叠{CONFIG['chunk_overlap']}字）")

    print("🧠 构建向量索引(FAISS) 与关键词索引(BM25) ...")
    faiss_index, _ = build_faiss_index(chunks)
    bm25_index = build_bm25_index(chunks)
    print(f"✅ 知识库就绪（{len(chunks)} 片已入库）。输入问题开始问答，输入 q 退出。\n"
          f"   当前 LLM 供应商: {CONFIG['llm_provider']}")

    # ---- 3. 命令行问答循环 ----
    while True:
        try:
            question = input("\n你问> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            continue
        if question.lower() in ("q", "quit", "exit"):
            break

        # ① 混合检索 Top3
        hits = hybrid_search(question, chunks, faiss_index, bm25_index)
        print(f"\n🔍 检索 Top{len(hits)}（融合得分）：")
        for h in hits:
            preview = h["text"][:40].replace("\n", " ")
            print(f"   [{h['rank']}] score={h['score']:.3f} | {preview}...")

        # ② 拼提示词 → ③ 调 LLM 生成
        answer = chat_llm(build_prompt(question, hits))
        print(f"\n🤖 回答：\n{answer}")


if __name__ == "__main__":
    main()
