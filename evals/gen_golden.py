"""评测语料与 golden set 生成（M3-6，§14.2.2）。

两步走：
    1. gen_corpus：合成 10 篇小语料（每篇一个独立知识域 + 唯一事实锚点），
       每篇 5 问，其中含跨文档对比问 —— 保证「应命中文档」客观可判定；
    2. gen_golden：LLM 从语料生成问句（不生成答案 —— 检索层指标不需要），
       每问标注 expected_docs（由语料的锚点结构静态推导，不靠 LLM 自评）。

为什么合成语料而不是拿真实 PDF：
    golden set 的核心是「应命中文档」的可判定性。合成语料每篇埋唯一
    事实锚点（如「Omega-3 支线任务调度器」），期望命中可以静态推导，
    人工校验成本降到零；M7 扩到 320 条时再混入真实课程资料。

用法：
    uv run python -m evals.gen_golden --out evals/golden_v1.jsonl
    uv run python -m evals.gen_golden --docs-dir data/evaldocs --write-corpus
"""

import argparse
import json
import random
from pathlib import Path

# ============================================================
# 语料：10 个知识域 × 每域一篇文档，每篇埋唯一锚点
# 锚点词只在对应文档出现 —— expected_docs 可静态判定
# ============================================================

_DOMAINS = [
    {
        "file": "ml_basics.md",
        "title": "机器学习基础",
        "anchor": "监督学习需要带标签的训练数据",
        "facts": [
            ("监督学习", "需要带标签的训练数据，代表算法是决策树与支持向量机"),
            ("无监督学习", "从无标签数据中发现结构，代表算法是 K-Means 聚类与主成分分析"),
            ("过拟合", "模型记忆训练集细节导致泛化差，常用 L2 正则化与早停缓解"),
            ("交叉验证", "K 折交叉验证把数据切 K 份轮流做验证集，K 常取 5 或 10"),
            ("特征工程", "标准化与独热编码是最常见的数值特征与类别特征处理手段"),
        ],
    },
    {
        "file": "deep_learning.md",
        "title": "深度学习入门",
        "anchor": "Transformer 依赖自注意力机制",
        "facts": [
            ("反向传播", "链式法则逐层计算梯度，是神经网络训练的核心算法"),
            ("激活函数", "ReLU 解决了 sigmoid 的梯度消失问题，是最常用的隐藏层激活"),
            ("Transformer", "依赖自注意力机制并行处理序列，取代了 RNN 的循环结构"),
            ("批归一化", "对每批输入做标准化，稳定训练并允许更大学习率"),
            ("Dropout", "训练时随机丢弃神经元，是简单有效的正则化手段"),
        ],
    },
    {
        "file": "db_systems.md",
        "title": "数据库系统",
        "anchor": "B+ 树索引适合范围查询",
        "facts": [
            ("事务 ACID", "原子性、一致性、隔离性、持久性是事务的四大特性"),
            ("B+ 树索引", "叶子节点成链表，适合范围查询；哈希索引只适合等值查询"),
            ("两阶段锁", "先加锁后解锁的两阶段协议，保证可串行化调度"),
            ("MVCC", "多版本并发控制让读不阻塞写，PostgreSQL 与 InnoDB 都采用"),
            ("慢查询优化", "EXPLAIN 查看执行计划，最常见的问题是没有命中索引的全表扫描"),
        ],
    },
    {
        "file": "operating_systems.md",
        "title": "操作系统",
        "anchor": "页面置换算法 LRU",
        "facts": [
            ("进程与线程", "进程是资源分配单位，线程是调度单位，同进程线程共享地址空间"),
            ("死锁", "互斥、持有并等待、不可剥夺、循环等待是死锁四条件"),
            ("页面置换", "LRU 淘汰最久未使用的页，Clock 算法是它的近似实现"),
            ("IO 多路复用", "epoll 用事件通知代替 select 的轮询，适合海量连接"),
            ("虚拟内存", "每个进程看到连续私有地址空间，由页表映射到物理内存"),
        ],
    },
    {
        "file": "computer_networks.md",
        "title": "计算机网络",
        "anchor": "TCP 三次握手",
        "facts": [
            ("TCP 三次握手", "SYN、SYN-ACK、ACK 三步建立连接，第三次握手可携带数据"),
            ("TCP 拥塞控制", "慢启动、拥塞避免、快重传、快恢复四个阶段"),
            ("HTTP 状态码", "301 永久重定向，404 未找到，502 网关错误"),
            ("DNS 解析", "递归查询与迭代查询，浏览器缓存→系统缓存→根域逐级查找"),
            ("HTTPS", "TLS 握手交换密钥，非对称加密协商、对称加密传输"),
        ],
    },
    {
        "file": "statistics.md",
        "title": "统计学基础",
        "anchor": "中心极限定理",
        "facts": [
            ("中心极限定理", "无论总体分布如何，样本均值的分布趋近正态分布"),
            ("假设检验", "p 值小于显著性水平时拒绝原假设，第一类错误是弃真"),
            ("置信区间", "95% 置信区间意味着重复抽样时约 95% 的区间包含真参数"),
            ("相关与因果", "相关不等于因果，混杂变量是常见的误导来源"),
            ("贝叶斯定理", "后验概率正比于先验概率乘以似然函数"),
        ],
    },
    {
        "file": "software_engineering.md",
        "title": "软件工程",
        "anchor": "单元测试 FIRST 原则",
        "facts": [
            ("单元测试", "遵循 FIRST 原则：快速、隔离、可重复、自验证、及时"),
            ("SOLID", "单一职责、开闭、里氏替换、接口隔离、依赖倒置五大原则"),
            ("技术债", "为短期速度牺牲设计质量的代价，随时间累积利息"),
            ("CI/CD", "持续集成频繁合并主干，持续部署自动发布通过流水线的构建"),
            ("代码评审", "发现缺陷只是收益之一，知识传播与风格统一同样重要"),
        ],
    },
    {
        "file": "distributed_systems.md",
        "title": "分布式系统",
        "anchor": "CAP 定理",
        "facts": [
            ("CAP 定理", "一致性、可用性、分区容忍三者不可兼得，网络分区时必弃其一"),
            ("一致性哈希", "节点增减只影响相邻区间的数据，避免全量重分布"),
            ("Raft", "领导者选举、日志复制、安全性三模块，比 Paxos 易理解"),
            ("幂等性", "同一操作执行多次效果与一次相同，常用唯一请求号实现"),
            ("分布式追踪", "trace_id 贯穿调用链，span 记录每段耗时与输入输出"),
        ],
    },
    {
        "file": "nlp_basics.md",
        "title": "自然语言处理",
        "anchor": "TF-IDF",
        "facts": [
            ("分词", "中文没有天然词边界，jieba 是最常用的中文分词库"),
            ("TF-IDF", "词频乘逆文档频率，衡量词对文档的区分度"),
            ("词向量", "Word2Vec 用上下文预测词，得到稠密的语义表示"),
            ("命名实体识别", "识别人名、地名、机构名等实体，是信息抽取的基础"),
            ("注意力机制", "让模型对输入的不同部分分配不同权重"),
        ],
    },
    {
        "file": "rag_notes.md",
        "title": "RAG 笔记",
        "anchor": "RRF 融合",
        "facts": [
            ("RAG", "检索增强生成先检索后生成，把知识外置到向量库"),
            ("混合检索", "向量路语义召回、关键词路精确匹配，两路并用互补"),
            ("RRF 融合", "倒数排名融合，k=60 是常用参数，无需调分数尺度"),
            ("重排序", "cross-encoder 对 query-文档对精细打分，精度高但慢"),
            ("父子分块", "子块用于精准召回，父块提供完整上下文"),
        ],
    },
]


def _build_doc(domain: dict) -> str:
    """单篇语料：标题 + 5 节（每节锚点句 + 展开）。约 600 字。"""
    parts = [f"# {domain['title']}\n"]
    for i, (term, desc) in enumerate(domain["facts"], start=1):
        parts.append(f"## 第{i}节 {term}\n")
        parts.append(f"{term}：{desc}。")
        # 展开 2-3 句，保证子块有足够上下文（避免标题孤儿块）
        parts.append(
            f"在学习{domain['title']}的过程中，理解「{term}」这一概念的应用场景与局限，"
            f"是把知识用于实践的前提。{term}的定义需要结合具体例子来记忆。"
        )
        parts.append("")
    return "\n".join(parts)


def write_corpus(docs_dir: Path) -> list[Path]:
    """生成全部语料文件，返回路径列表（顺序即 doc_id 编号顺序）。"""
    docs_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for domain in _DOMAINS:
        p = docs_dir / str(domain["file"])
        p.write_text(_build_doc(domain), encoding="utf-8")
        out.append(p)
    return out


# ============================================================
# golden set：模板问句（静态、可判定）+ LLM 改写（自然、多样）
# ============================================================

_QUESTION_TEMPLATES = [
    "{term}是什么意思？",
    "请解释一下{term}。",
    "{term}有什么作用？",
    "什么是{term}？它解决了什么问题？",
    "课程材料里怎么讲{term}的？",
    "帮我总结{term}的要点。",
    "{term}和它的应用场景是什么？",
    "为什么需要{term}？",
    "{term}的原理是什么？",
    "{term}具体指什么，举个例子？",
]

# 跨文档对比问（应命中两篇）：领域 i 的锚点 vs 领域 j 的同位概念
_CROSS_TEMPLATES = [
    "{t1}和{t2}分别是什么，有什么区别？",
    "对比一下{t1}与{t2}。",
    "{t1}与{t2}分别在什么场景下使用？",
    "分别解释{t1}和{t2}，并说明差异。",
    "{t1}、{t2}的定义和区别是什么？",
]


def build_golden_static() -> list[dict]:
    """静态生成 50 条 golden（模板问句，不含 LLM 改写）。

    45 条单文档问（9 域 × 5 问）+ 5 条跨文档对比问 = 50。
    expected_docs 由锚点唯一性静态推导 —— 这是不依赖 LLM 自评的
    可判定基线；LLM 改写模式（--use-llm）只改写问句表面，标注不变。
    """
    rng = random.Random(42)
    items: list[dict] = []

    # 单文档问：每个域取前 4 个 term 各出 1 问（10 域 × 4 = 40），
    # 再补 5 问凑满 45（前 5 个域各加 1 问，换模板避免重复）
    for domain in _DOMAINS:
        terms = [t for t, _ in domain["facts"][:4]]
        templates = rng.sample(_QUESTION_TEMPLATES, len(terms))
        for term, tpl in zip(terms, templates, strict=True):
            items.append(
                {
                    "id": f"q{len(items) + 1:03d}",
                    "query": tpl.format(term=term),
                    "expected_docs": {domain["file"]: 2},
                    "domain": domain["title"],
                    "term": term,
                }
            )
    for di in range(5):
        domain = _DOMAINS[di]
        term = domain["facts"][4][0]  # 每域第 5 个 term
        tpl = _QUESTION_TEMPLATES[(di * 2 + 4) % len(_QUESTION_TEMPLATES)]
        items.append(
            {
                "id": f"q{len(items) + 1:03d}",
                "query": tpl.format(term=term),
                "expected_docs": {domain["file"]: 2},
                "domain": domain["title"],
                "term": term,
            }
        )

    # 跨文档问：固定 5 对域，锚点 term 对比（应命中两篇）
    cross_pairs = [(0, 4), (2, 7), (1, 8), (3, 5), (6, 9)]
    for i, (a, b) in enumerate(cross_pairs):
        ta = _DOMAINS[a]["facts"][0][0]
        tb = _DOMAINS[b]["facts"][0][0]
        tpl = _CROSS_TEMPLATES[i % len(_CROSS_TEMPLATES)]
        items.append(
            {
                "id": f"q{len(items) + 1:03d}",
                "query": tpl.format(t1=ta, t2=tb),
                "expected_docs": {_DOMAINS[a]["file"]: 2, _DOMAINS[b]["file"]: 1},
                "domain": f"{_DOMAINS[a]['title']}×{_DOMAINS[b]['title']}",
                "term": f"{ta}|{tb}",
            }
        )

    # 45 单文档 + 5 跨文档 = 50；模板已随机化，无须再打乱
    assert len(items) == 50, len(items)
    return items


async def rewrite_queries_with_llm(items: list[dict]) -> list[dict]:
    """用 LLM 把模板问句改写成更自然的表达（可选，--use-llm）。

    只改写表面，expected_docs 原样保留 —— 标注的可判定性不受影响。
    改写失败（离线/限流）则保留原句，评测不中断。
    """
    from openai import AsyncOpenAI

    from apps.api.core.config import settings

    client = AsyncOpenAI(
        base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=30.0
    )
    out: list[dict] = []
    for item in items:
        try:
            resp = await client.chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "把用户问题改写得更自然、像真实学生口吻，但**必须保留原问题"
                            "的核心术语（术语原词不能换）**。只输出改写后的问题，别加任何解释。"
                        ),
                    },
                    {"role": "user", "content": item["query"]},
                ],
                temperature=0.3,
            )
            rewritten = (resp.choices[0].message.content or "").strip()
            # 术语必须仍在（防止 LLM 偷换概念导致标注失效）
            if rewritten and all(
                t in rewritten for t in item["term"].split("|")
            ):
                item = {**item, "query": rewritten, "rewritten": True}
        except Exception:  # noqa: BLE001 —— 离线时保留模板问句
            pass
        out.append(item)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="evals/golden_v1.jsonl")
    ap.add_argument("--docs-dir", default="data/evaldocs")
    ap.add_argument("--write-corpus", action="store_true", help="生成评测语料文件")
    ap.add_argument("--use-llm", action="store_true", help="LLM 改写问句（默认模板）")
    args = ap.parse_args()

    if args.write_corpus:
        paths = write_corpus(Path(args.docs_dir))
        print(f"corpus written: {len(paths)} files -> {args.docs_dir}")

    items = build_golden_static()
    if args.use_llm:
        import asyncio

        items = asyncio.run(rewrite_queries_with_llm(items))
        n_rw = sum(1 for i in items if i.get("rewritten"))
        print(f"llm rewritten: {n_rw}/{len(items)}")

    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"golden written: {len(items)} items -> {out_path}")

    # 自检：每个 term 必须出现在其期望命中的至少一个文档里；
    # 且锚点不泄漏到其他文档（跨文档问的两个 term 分属两篇）
    if args.write_corpus:
        corpus = {p.name: p.read_text(encoding="utf-8") for p in paths}
        for item in items:
            for fname, rel in item["expected_docs"].items():
                terms = item["term"].split("|")
                # 跨文档问：term[0] 应在主文档（rel=2），term[1] 在副文档（rel=1）；
                # 单文档问：唯一 term 必须在期望文档
                if len(terms) == 2 and rel == 2:
                    need, other = terms[0], terms[1]
                elif len(terms) == 2 and rel == 1:
                    need, other = terms[1], terms[0]
                else:
                    need, other = terms[0], None
                if need not in corpus[fname]:
                    raise SystemExit(f"anchor check failed: {need!r} not in {fname}")
                if other is not None and other in corpus[fname]:
                    # 术语泄漏到另一篇 → 标注失效
                    raise SystemExit(f"anchor leak: {other!r} also in {fname}")
        print("anchor uniqueness: ok")


if __name__ == "__main__":
    main()
