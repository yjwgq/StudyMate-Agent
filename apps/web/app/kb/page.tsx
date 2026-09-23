'use client';

/**
 * 知识库页（M3-8，§12.1）：文档列表 / 上传 / 入库进度。
 *
 * 交互要点：
 *   - 上传后轮询状态（M2 用轮询而非推送，M5 接入 SSE 通知）；
 *   - processing 显示进度态、failed 展示 error_message 与重跑入口；
 *   - 顶部导航「检索调试台」与「对话」互相可达。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { apiFetch, readableError } from '../../lib/api';
import { clearTokens, getEmail, isLoggedIn } from '../../lib/auth';

interface Doc {
  id: string;
  title: string;
  status: 'processing' | 'ready' | 'failed';
  parser_used?: string | null;
  chunk_count?: number | null;
  error_message?: string | null;
  created_at: string;
}

export default function KbPage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [email, setEmail] = useState<string | null>(null);
  const [docs, setDocs] = useState<Doc[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const pollingRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    const res = await apiFetch('/api/v1/kb/documents');
    if (res.status === 401) return; // apiFetch 已跳登录
    if (!res.ok) {
      setMsg(await readableError(res));
      return;
    }
    const body = await res.json();
    setDocs(body?.data?.documents ?? []);
  }, []);

  useEffect(() => {
    if (!isLoggedIn()) {
      router.replace('/login');
      return;
    }
    setEmail(getEmail());
    setReady(true);
    void load();
  }, [router, load]);

  // 有 processing 文档时轮询（2s），全部结束停表
  useEffect(() => {
    const hasProcessing = docs.some((d) => d.status === 'processing');
    if (!hasProcessing) {
      if (pollingRef.current) {
        window.clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
      return;
    }
    if (pollingRef.current) return;
    pollingRef.current = window.setInterval(() => void load(), 2000);
    return () => {
      if (pollingRef.current) {
        window.clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    };
  }, [docs, load]);

  function logout() {
    clearTokens();
    router.push('/login');
  }

  async function upload(file: File) {
    setBusy(true);
    setMsg(null);
    try {
      const form = new FormData();
      form.append('file', file);
      // 注意：不要手写 Content-Type —— 浏览器要自己带 boundary
      const res = await apiFetch('/api/v1/kb/documents', { method: 'POST', body: form });
      if (!res.ok) {
        setMsg(await readableError(res));
        return;
      }
      const body = await res.json();
      const d = body?.data?.document;
      setMsg(
        body?.data?.duplicated
          ? `该文件已存在（内容一致），复用文档「${d?.title}」。`
          : `已上传「${d?.title}」，正在后台解析入库…`,
      );
      await load();
    } catch (err) {
      setMsg(`上传失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  }

  async function reingest(id: string) {
    setBusy(true);
    setMsg(null);
    try {
      const res = await apiFetch(`/api/v1/kb/documents/${id}/reingest`, { method: 'POST' });
      if (!res.ok) {
        setMsg(await readableError(res));
        return;
      }
      setMsg('已重新投递入库任务。');
      await load();
    } finally {
      setBusy(false);
    }
  }

  if (!ready) return null;

  return (
    <div className="layout wide">
      <header className="header">
        <div className="header-row">
          <div>
            <h1>知识库</h1>
            <p>M3 · 上传后自动解析/分块/向量化入库；入库完成后即可在对话中检索引用</p>
          </div>
          <div className="header-user">
            <span>{email ?? '已登录'}</span>
            <Link className="linklike" href="/kb/debug">检索调试台</Link>
            <Link className="linklike" href="/">对话</Link>
            <button className="linklike" onClick={logout}>退出</button>
          </div>
        </div>
      </header>

      <div className="kb-toolbar">
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.docx,.md,.txt,.html"
          style={{ display: 'none' }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void upload(f);
          }}
        />
        <button
          className="primary"
          disabled={busy}
          onClick={() => fileRef.current?.click()}
        >
          {busy ? '处理中…' : '上传文档'}
        </button>
        <span className="hint">支持 PDF / DOCX / MD / TXT / HTML，单文件 ≤ 50MB</span>
      </div>

      {msg && <div className="kb-msg">{msg}</div>}

      <div className="kb-table">
        <div className="kb-row kb-head">
          <span>标题</span>
          <span>状态</span>
          <span>分块</span>
          <span>解析器</span>
          <span>操作</span>
        </div>
        {docs.length === 0 ? (
          <div className="empty" style={{ padding: '32px 0' }}>
            还没有文档。上传一份 PDF 或 Markdown 试试。
          </div>
        ) : (
          docs.map((d) => (
            <div className="kb-row" key={d.id}>
              <span className="kb-title" title={d.title}>{d.title}</span>
              <span>
                <span className={`status ${d.status}`}>
                  {d.status === 'processing' ? '入库中' : d.status === 'ready' ? '就绪' : '失败'}
                </span>
                {d.status === 'processing' && <span className="spinner" />}
              </span>
              <span>{d.chunk_count ?? '—'}</span>
              <span>{d.parser_used ?? '—'}</span>
              <span>
                <button className="linklike" disabled={busy} onClick={() => void reingest(d.id)}>
                  重跑
                </button>
              </span>
              {d.status === 'failed' && d.error_message && (
                <span className="kb-error">{d.error_message.slice(0, 160)}</span>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}