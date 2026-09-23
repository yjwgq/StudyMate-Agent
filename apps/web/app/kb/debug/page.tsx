'use client';

/**
 * 检索调试台（M3-8，§8.2）。
 *
 * 与 chat 共用同一 Retriever：这里看到的排序就是问答时注入上下文的顺序。
 * 用途：
 *   1. 调参/排查检索质量问题（分数、命中父块、来源文档）；
 *   2. M6 加混合检索 + RRF + Rerank 后，本页是逐路对比的观察窗。
 */

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { apiFetch, readableError } from '../../../lib/api';
import { getEmail, isLoggedIn } from '../../../lib/auth';

interface Hit {
  n: number;
  score: number;
  document_id: string | null;
  title: string;
  page: number | null;
  snippet: string;
  parent_content: string;
}

export default function KbDebugPage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [email, setEmail] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState(6);
  const [hits, setHits] = useState<Hit[] | null>(null);
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
      setElapsed(performance.now() - t0);
    } catch (err) {
      setMsg(`检索失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  if (!ready) return null;

  const maxScore = hits && hits.length > 0 ? Math.max(...hits.map((h) => h.score)) : 0;

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
                  <span className="hit-score" title="cosine 相似度">
                    {h.score.toFixed(4)}
                  </span>
                </div>
                <div className="hit-bar">
                  <div
                    className="hit-bar-fill"
                    style={{ width: `${maxScore > 0 ? (h.score / maxScore) * 100 : 0}%` }}
                  />
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