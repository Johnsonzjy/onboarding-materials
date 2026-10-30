# -*- coding: utf-8 -*-
"""
RAG 智能模块：复用阶段 1 核心算法并封装为服务级组件
- 文档加载 → 500 字切片 → FAISS 向量索引 + BM25 关键词索引
- BM25 + 向量混合检索（加权融合 Top3）
- LLM 流式调用（供应商可配置，mock 模式支持离线演示）
"""
import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import AsyncGenerator

import faiss
import jieba
import numpy as np
from rank_bm25 import BM25Okapi

# =====================================================================
# 配置（环境变量可覆盖；默认 mock，保证零 Key 离线跑通）
# =====================================================================
PROVIDER_PRESETS = {
    "openai":   {"base_url": "https://api.openai.com/v1",                "model": "gpt-4o-mini"},
    "deepseek": {"base_url": "https://api.deepseek.com",                 "model": "deepseek-chat"},
    "qwen":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "glm":      {"base_url": "https://open.bigmodel.cn/api/paas/v4",     "model": "glm-4-flash"},
}

CONFIG = {
    "llm_provider": os.getenv("LLM_PROVIDER", "mock"),
    "llm_api_key":  os.getenv("LLM_API_KEY", ""),
    "llm_model":    os.getenv("LLM_MODEL", ""),
    "llm_base_url": os.getenv("LLM_BASE_URL", ""),
    "embed_api_key":  os.getenv("EMBED_API_KEY", ""),
    "embed_model":    os.getenv("EMBED_MODEL", "text-embedding-3-small"),
    "embed_base_url": os.getenv("EMBED_BASE_URL", "https://api.openai.com/v1"),
    "chunk_size": 500,        # 需求：500 字切片
    "chunk_overlap": 50,      # 片间重叠，防语义截断
    "top_k": 3,               # 需求：混合检索 Top3
    "bm25_weight": 0.5,       # 融合权重：BM25 占比
    "local_embed_dim": 256,   # 本地降级向量维度（仅离线演示）
}

# 问答 system 提示词：约束模型只依据制度片段作答并标注引用
SYSTEM_PROMPT = (
    "你是公司制度问答助手，须严格遵守：\n"
    "1. 只依据提供的制度片段回答，不编造片段外的信息；\n"
    "2. 片段中没有的内容，明确回答'现行制度中未查到'；\n"
    "3. 涉及金额/流程按片段原文表述，回答末尾用 [编号] 标注引用片段。"
)


# =====================================================================
# 文本处理：切片 / 分词 / 向量化
# =====================================================================

def split_to_chunks(text: str, max_len: int = None, overlap: int = None) -> list[str]:
    """500 字滑窗切片：按空行分段合并，超长段落滑窗硬切（★复用阶段1核心★）"""
    max_len = max_len or CONFIG["chunk_size"]
    overlap = overlap if overlap is not None else CONFIG["chunk_overlap"]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paragraphs:
        if len(buf) + len(p) + 1 <= max_len:
            buf = f"{buf}\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= max_len:
                buf = p
            else:
                start = 0
                while start < len(p):
                    chunks.append(p[start:start + max_len])
                    start += max_len - overlap
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def _tokenize(text: str) -> list[str]:
    """中文分词（BM25 与查询共用，保证词表一致）"""
    return [w for w in jieba.lcut(text) if w.strip()]


def _l2norm(vecs: np.ndarray) -> np.ndarray:
    """L2 归一化：使 FAISS 内积 = 余弦相似度"""
    norm = np.linalg.norm(vecs, axis=1, keepdims=True)
    norm[norm == 0] = 1
    return (vecs / norm).astype("float32")


def _local_embed(texts: list[str]) -> np.ndarray:
    """本地降级向量（字符 n-gram 哈希）：无需密钥，仅离线演示用（★复用阶段1★）"""
    dim = CONFIG["local_embed_dim"]
    vecs = np.zeros((len(texts), dim), dtype="float32")
    for i, t in enumerate(texts):
        feats = [t[j:j + n] for n in (1, 2) for j in range(len(t) - n + 1)]
        for f in feats:
            h = int(hashlib.md5(f.encode("utf-8")).hexdigest(), 16)
            vecs[i, h % dim] += 1 if (h >> 128) % 2 == 0 else -1
    return _l2norm(vecs)


def embed_texts(texts: list[str]) -> np.ndarray:
    """文本 → 归一化向量矩阵：优先 Embedding API，无密钥自动降级本地（★复用阶段1★）"""
    if not CONFIG["embed_api_key"]:
        return _local_embed(texts)
    from openai import OpenAI
    client = OpenAI(api_key=CONFIG["embed_api_key"], base_url=CONFIG["embed_base_url"])
    all_vecs = []
    for i in range(0, len(texts), 16):               # 分批防超限
        batch = texts[i:i + 16]
        resp = client.embeddings.create(model=CONFIG["embed_model"], input=batch)
        all_vecs.extend([d.embedding for d in resp.data])
    return _l2norm(np.array(all_vecs, dtype="float32"))


def _minmax(arr: np.ndarray) -> np.ndarray:
    """min-max 归一化到 [0,1]，使两路分数可加权融合"""
    lo, hi = arr.min(), arr.max()
    return (arr - lo) / (hi - lo) if hi > lo else np.zeros_like(arr)


# =====================================================================
# 知识库：加载 → 建双索引 → 混合检索
# =====================================================================

class KnowledgeBase:
    """进程级知识库单例：启动时构建一次，问答时只读检索"""

    def __init__(self):
        self.chunks: list[str] = []      # 切片正文
        self.sources: list[str] = []     # 每个切片所属文件名（引用展示用）
        self.index = None                # FAISS 内积索引
        self.bm25: BM25Okapi | None = None
        self.ready = False               # 是否就绪（/health 与 /chat 校验用）

    def build(self, kb_dir: Path) -> int:
        """扫描目录下全部 .md 制度文件，切片并构建双索引，返回切片数"""
        self.chunks, self.sources = [], []
        for md in sorted(Path(kb_dir).glob("*.md")):
            text = md.read_text(encoding="utf-8")
            for piece in split_to_chunks(text):
                self.chunks.append(piece)
                self.sources.append(md.name)
        if not self.chunks:
            return 0
        vecs = embed_texts(self.chunks)
        self.index = faiss.IndexFlatIP(vecs.shape[1])
        self.index.add(vecs)
        self.bm25 = BM25Okapi([_tokenize(c) for c in self.chunks])
        self.ready = True
        return len(self.chunks)

    def hybrid_search(self, question: str, top_k: int = None) -> list[dict]:
        """BM25 + 向量加权融合检索 TopK（★复用阶段1核心★）

        返回 [{rank, score, text, source}]，source 为制度文件名。
        """
        if not self.ready:
            return []
        top_k = top_k or CONFIG["top_k"]
        n = len(self.chunks)

        # 路 1：向量语义召回（FAISS 内积 = 余弦）
        q_vec = embed_texts([question])
        scores_vec = np.full(n, -1.0, dtype="float32")
        D, I = self.index.search(q_vec, n)
        for s, idx in zip(D[0], I[0]):
            if idx >= 0:
                scores_vec[idx] = s

        # 路 2：BM25 关键词召回
        scores_bm25 = np.array(self.bm25.get_scores(_tokenize(question)), dtype="float32")

        # 加权融合取 TopK
        fused = (CONFIG["bm25_weight"] * _minmax(scores_bm25)
                 + (1 - CONFIG["bm25_weight"]) * _minmax(scores_vec))
        top_idx = np.argsort(fused)[::-1][:top_k]
        return [{"rank": r + 1, "score": float(fused[i]),
                 "text": self.chunks[i], "source": self.sources[i]}
                for r, i in enumerate(top_idx)]


def build_prompt(question: str, hits: list[dict]) -> str:
    """把 Top3 片段拼入提示词（★复用阶段1核心★）"""
    ctx = "\n\n".join(f"[{h['rank']}] {h['text']}" for h in hits)
    return (
        "请严格依据以下制度知识库片段回答用户问题。\n"
        "要求：\n"
        "1. 回答末尾用 [编号] 标注引用了哪些片段；\n"
        "2. 片段中没有的信息不要编造，明确说明'现行制度中未查到'；\n"
        "3. 涉及金额/流程的，按片段原文表述。\n\n"
        f"参考片段：\n{ctx}\n\n"
        f"用户问题：{question}"
    )


# 进程级单例：main.py 启动时调用 KB.build()
KB = KnowledgeBase()


# =====================================================================
# LLM 流式调用（供应商可配置）
# =====================================================================

def _mock_answer(question: str, hits: list[dict]) -> str:
    """mock 模式：抽取式伪回答——引用片段前 80 字拼接，验证全链路用"""
    lines = [f"[{h['rank']}] {h['text'][:80].replace(chr(10), ' ')}…" for h in hits[:2]]
    return (f"关于「{question}」，根据制度知识库检索结果（MOCK 模式，仅验证链路）：\n"
            + "\n".join(lines))


async def stream_llm(history: list[dict], question: str, hits: list[dict]) -> AsyncGenerator[str, None]:
    """流式生成回答，逐段 yield 文本增量。

    - mock：把伪回答切成 2~4 字小段输出，模拟打字机节奏
    - 真实供应商：OpenAI 兼容接口 stream=True，透传 delta
    """
    provider = CONFIG["llm_provider"]

    if provider == "mock":
        answer = _mock_answer(question, hits)
        for i in range(0, len(answer), 3):            # 每次吐 3 个字
            yield answer[i:i + 3]
            await asyncio.sleep(0.03)                 # 小延迟模拟真实流式
        return

    # ---- 真实供应商：组装 system + 6轮历史 + 本轮 RAG 提示词，流式输出 ----
    from openai import AsyncOpenAI
    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise ValueError(f"未知 LLM 供应商: {provider}")
    client = AsyncOpenAI(
        api_key=CONFIG["llm_api_key"] or "EMPTY",
        base_url=CONFIG["llm_base_url"] or preset["base_url"],
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": m["role"], "content": m["content"]}
                 for m in history if m["role"] in ("user", "assistant")]
    messages.append({"role": "user", "content": build_prompt(question, hits)})

    stream = await client.chat.completions.create(
        model=CONFIG["llm_model"] or preset["model"],
        messages=messages,
        temperature=0.3,
        stream=True,
    )
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
