'use client';

/**
 * 检索调试台（M3-8 → M6 升级为逐路对比视图）。
 *
 * 与 chat 共用同一 Retriever：这里看到的排序就是问答时注入上下文的顺序。
 * 用途：
 *   1. 调参/排查检索质量问题（分数、命中父块、来源文档）；
 *   2. **逐路对比**（M6）：每命中显示向量分 / 关键词分 / RRF 分 / 精排分与
 *      命中的路数 —— 判断「混合检索贡献了什么、精排改变了什么排序」；
 *   3. **降级观察**：页面顶部展示本次生效的 flag 与降级标记，配合
 *      /meta/flags 开关即可现场演示「一路挂了还能用」（G2/G3）。
 */

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { apiFetch, readableError } from '../../../lib/api';
import { getEmail, isLoggedIn } from '../../../lib/auth';

interface Hit {
  n: number;
  score: number;
  rerank_score: number | null;
  vector_score: number | null;
  keyword_score: number | null;
  rrf_score: number | null;
  sources: string[];
  document_id: string | null;
  title: string;
  page: number | null;
  snippet: string;
  parent_content: string;
}

interface Diagnostics {
  flags?: Record<string, boolean>;
  vector_count?: number;
  keyword_count?: number;
  fused_count?: number;
  vector_ms?: number | null;
  keyword_ms?: number | null;
  rerank_ms?: number | null;
}

const DEGRADED_TEXT: Record<string, string> = {
  rerank: '未精排（精排不可用，已用 RRF 顺序）',
  vector: '未走向量检索（仅关键词路）',
  keyword: '未走关键词检索（仅向量路）',
  retrieval: '本轮未使用知识库',
};

const fmt = (v: number | null | undefined, digits = 4) => (v == null ? '—' : v.toFixed(digits));

export default function KbDebugPage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [email, setEmail] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState(6);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [degraded, setDegraded] = useState<string[]>([]);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);

  useEffect(() => {
    if (!isLoggedIn()) {
      router.replace('/login');
      return;
    }
    setEmail(getEmail());
    setReady(true);
  }, [router]);

  async function search() {
    const q = query.trim();
    if (!q || busy) return;
    setBusy(true);
    setMsg(null);
    setHits(null);
    setDegraded([]);
    setDiag(null);
    setExpanded(null);
    const t0 = performance.now();
    try {
      const res = await apiFetch('/api/v1/kb/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q, top_k: topK }),
      });
      if (!res.ok) {
        setMsg(await readableError(res));
        return;
      }
      const body = await res.json();
      setHits(body?.data?.hits ?? []);
      setDegraded(body?.data?.degraded ?? []);
      setDiag(body?.data?.diagnostics ?? null);
      setElapsed(performance.now() - t0);
    } catch (err) {
      setMsg(`检索失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  if (!ready) return null;

  // 进度条基准用「展示分」（精排分优先）—— 否则精排分与向量分混在一张图上会失真
  const maxScore =
    hits && hits.length > 0
      ? Math.max(...hits.map((h) => h.rerank_score ?? h.score))
      : 0;

  return (
    <div className="layout wide">
      <header className="header">
        <div className="header-row">
          <div>
            <h1>检索调试台</h1>
            <p>单路向量检索（M3）· 与对话共用同一管线，观察命中的父块与相似度</p>
          </div>
          <div className="header-user">
            <span>{email ?? '已登录'}</span>
            <Link className="linklike" href="/kb">知识库</Link>
            <Link className="linklike" href="/">对话</Link>
          </div>
        </div>
      </header>

      <div className="kb-toolbar">
        <input
          className="debug-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              void search();
            }
          }}
          placeholder="输入检索问题，Enter 检索（如：什么是监督学习？）"
        />
        <label className="debug-k">
          top_k
          <select value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
            {[3, 6, 10, 20].map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </label>
        <button className="primary" disabled={busy || !query.trim()} onClick={() => void search()}>
          {busy ? '检索中…' : '检索'}
        </button>
      </div>

      {msg && <div className="kb-msg">{msg}</div>}

      {hits !== null && (
        <div className="debug-results">
          {(degraded.length > 0 || diag?.flags) && (
            <div className={`debug-flags ${degraded.length > 0 ? 'degraded' : ''}`}>
              <div className="debug-flag-row">
                <span className="debug-flag-label">生效开关</span>
                {Object.entries(diag?.flags ?? {}).map(([k, v]) => (
                  <span key={k} className={`flag-chip ${v ? 'on' : 'off'}`} title={k}>
                    {k.replace('retrieval.', '').replace('.enabled', '')}={v ? 'on' : 'off'}
                  </span>
                ))}
              </div>
              {diag && (
                <div className="debug-flag-row">
                  <span className="debug-flag-label">逐路</span>
                  <span className="flag-chip">
                    向量 {diag.vector_count ?? 0} 条 / {diag.vector_ms ?? '—'}ms
                  </span>
                  <span className="flag-chip">
                    关键词 {diag.keyword_count ?? 0} 条 / {diag.keyword_ms ?? '—'}ms
                  </span>
                  <span className="flag-chip">融合 {diag.fused_count ?? 0} 条</span>
                  <span className="flag-chip">精排 {diag.rerank_ms ?? '—'}ms</span>
                </div>
              )}
              {degraded.length > 0 && (
                <div className="debug-degraded">
                  ⚠ 本轮降级：{degraded.map((f) => DEGRADED_TEXT[f] ?? f).join('；')}
                </div>
              )}
            </div>
          )}
          <div className="debug-meta">
            命中 {hits.length} 条
            {elapsed !== null && ` · 端到端 ${Math.round(elapsed)}ms`}
          </div>
          {hits.length === 0 ? (
            <div className="empty" style={{ padding: '32px 0' }}>
              没有命中。确认知识库里已有「就绪」状态的文档，或换个说法试试。
            </div>
          ) : (
            hits.map((h) => (
              <div className="hit-card" key={h.n}>
                <div className="hit-head">
                  <span className="hit-rank">[{h.n}]</span>
                  <span className="hit-title">{h.title}</span>
                  {h.page !== null && <span className="hit-page">第 {h.page} 段</span>}
                  {h.sources.map((src) => (
                    <span key={src} className={`src-chip ${src}`}>
                      {src === 'vector' ? '向量' : '关键词'}
                    </span>
                  ))}
                  <span className="hit-score" title={h.rerank_score != null ? '精排分' : '融合/向量分'}>
                    {fmt(h.rerank_score ?? h.score)}
                  </span>
                </div>
                <div className="hit-bar">
                  <div
                    className="hit-bar-fill"
                    style={{
                      width: `${maxScore > 0 ? ((h.rerank_score ?? h.score) / maxScore) * 100 : 0}%`,
                    }}
                  />
                </div>
                <div className="hit-scores">
                  <span>向量 <b>{fmt(h.vector_score)}</b></span>
                  <span>关键词 <b>{fmt(h.keyword_score)}</b></span>
                  <span>RRF <b>{h.rrf_score == null ? '—' : h.rrf_score.toFixed(5)}</b></span>
                  <span>精排 <b>{fmt(h.rerank_score)}</b></span>
                </div>
                <div className="hit-snippet">{h.snippet}</div>
                <button className="linklike" onClick={() => setExpanded(expanded === h.n ? null : h.n)}>
                  {expanded === h.n ? '收起父块' : '查看注入的父块全文'}
                </button>
                {expanded === h.n && <pre className="hit-parent">{h.parent_content}</pre>}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}