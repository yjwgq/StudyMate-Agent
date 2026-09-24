"""混合检索的纯函数部分（M6-1/M6-2，§8.2）。

放纯函数在这里是为了**可单测**：RRF 的数学与 tsquery 的净化都是易错、
且在集成环境里难以精准断言的逻辑（同分并列、去重、元字符注入）。

两块内容：
    build_tsquery(query)  —— 查询侧 jieba 分词 + tsquery 净化（与入库侧同一分词器）
    rrf_fuse(paths)       —— 倒数排名融合（k=60），按 child chunk_id 去重并记录各路贡献

为什么查询侧要 jieba（ADR-2）：
    PostgreSQL 原生不支持中文分词，我们用「应用层 jieba 分词写入 content_tokens
    → tsv 生成列」的方案。查询侧必须用**同一分词器**，否则「监督学习」
    查询词与写入的 token 对不上（tsvector 里是「监督 学习」，查询词若是整串
    就永远匹配不到）。
"""

import contextlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# tsquery 语法中的元字符（& | ! ( ) : * <-> 等）与引号：直接拼进 tsquery 会语法错误
# 或被当作操作符（可构造出非预期的查询语义）—— 必须净化
_TSQUERY_UNSAFE = re.compile(r"[&|!()<>:*'\"\\\-]")
# 仅保留中文、字母、数字（其余一律丢弃：标点、空白、表情等）
_TOKEN_KEEP = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
# 单字停用（中文单字检索噪声大：的/了/是…）
_STOPWORDS = frozenset(["的", "了", "是", "在", "和", "与", "或", "及", "也", "都", "就", "我", "你", "他", "它", "们", "这", "那", "有", "为", "对", "从", "到", "中"])


def tokenize(query: str) -> list[str]:
    """查询侧分词（与入库侧 worker/tasks/ingest.py::_jieba_tokens 同一分词器）。

    返回净化后的 token 列表（去停用词、去重、保序）。
    """
    import jieba

    tokens: list[str] = []
    seen: set[str] = set()
    for raw in jieba.lcut(query):
        # 净化：只保留中文/字母/数字（顺带把 tsquery 元字符全部剔除）
        for piece in _TOKEN_KEEP.findall(raw):
            if not piece or piece in _STOPWORDS:
                continue
            key = piece.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(piece)
    return tokens


def build_tsquery(query: str, *, max_tokens: int = 32) -> str:
    """构造 tsquery 字符串（`tok1 | tok2 | ...`，OR 语义以保召回）。

    - OR 而非 AND：召回阶段宁可宽（多召回几条），精度交给后面的 RRF 与精排；
      AND 会把「监督学习的应用场景」这类多词查询收得太紧，长尾词直接漏光。
    - 空结果（全停用词 / 纯标点）返回空串 —— 调用方据此跳过关键词路。
    """
    tokens = tokenize(query)[:max_tokens]
    if not tokens:
        return ""
    # 双保险：token 已按白名单净化，这里再挡一次元字符
    safe = [_TSQUERY_UNSAFE.sub("", t) for t in tokens]
    safe = [t for t in safe if t]
    if not safe:
        return ""
    return " | ".join(safe)


@dataclass
class FusedCandidate:
    """RRF 融合后的一个候选（child chunk 级）。"""

    chunk_id: str
    rrf_score: float = 0.0
    vector_rank: int | None = None   # 向量路排名（1-based）
    keyword_rank: int | None = None  # 关键词路排名（1-based）
    # 各路原始分（调试台逐路对比必需）——注意不能只留 merged payload 里的 score：
    # 两路都命中时 payload 只保留其中一路的 score（M6 冒烟实锤：keyword_score 恒 None）
    path_scores: dict[str, float] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def sources(self) -> list[str]:
        out = []
        if self.vector_rank is not None:
            out.append("vector")
        if self.keyword_rank is not None:
            out.append("keyword")
        return out


def rrf_fuse(
    paths: dict[str, list[dict[str, Any]]],
    *,
    k: int = 60,
    id_key: str = "chunk_id",
    limit: int | None = None,
) -> list[FusedCandidate]:
    """倒数排名融合（Reciprocal Rank Fusion，§8.2 的 k=60）。

        score(d) = Σ_path 1 / (k + rank_path(d))     rank 从 1 开始

    为什么用 RRF 而不是加权分数和：向量余弦分（0~1）与 ts_rank_cd（无上界、
    量纲不同）无法直接相加；RRF 只看**排名**，天然免调参、免归一化 ——
    这是它成为混合检索默认融合策略的原因。

    paths 的 value 是有序列表（已按各路分数降序），元素必须含 id_key 字段；
    先出现的路径在**平分**时优先（保持调用方给出的优先级，便于稳定复现）。
    """
    if k <= 0:
        raise ValueError("RRF 的 k 必须为正数")
    fused: dict[str, FusedCandidate] = {}
    order: list[str] = []
    scored: set[tuple[str, str]] = set()  # (path, chunk_id)：每路只计一次最佳排名
    for path_name, items in paths.items():
        for rank, item in enumerate(items, start=1):
            cid = item.get(id_key)
            if not cid:
                continue
            cid = str(cid)
            cand = fused.get(cid)
            if cand is None:
                cand = FusedCandidate(chunk_id=cid, payload=dict(item))
                fused[cid] = cand
                order.append(cid)
            # 多路命中时保留信息更全的 payload（字段更全的那份）
            elif len(item) > len(cand.payload):
                cand.payload = {**cand.payload, **item}
            # 同一路里的重复候选只计一次（否则重复项会把分数刷高 —— 单测覆盖）
            if (path_name, cid) in scored:
                continue
            scored.add((path_name, cid))
            cand.rrf_score += 1.0 / (k + rank)
            raw_score = item.get("score")
            if raw_score is not None:
                with contextlib.suppress(TypeError, ValueError):
                    cand.path_scores[path_name] = float(raw_score)
            if path_name == "vector":
                cand.vector_rank = cand.vector_rank or rank
            elif path_name == "keyword":
                cand.keyword_rank = cand.keyword_rank or rank
            else:
                cand.payload.setdefault("other_ranks", {})[path_name] = rank

    # 稳定排序：分数降序，同分按首次出现顺序（避免评测抖动）
    rank_of = {cid: i for i, cid in enumerate(order)}
    out = sorted(fused.values(), key=lambda c: (-c.rrf_score, rank_of[c.chunk_id]))
    return out[:limit] if limit else out
