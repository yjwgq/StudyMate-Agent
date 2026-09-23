'use client';

/**
 * 聊天页 —— M5 版本：执行与传输解耦 + HITL 审批（§7.7 / §7.3）。
 *
 * 相对 M3/M4 的变化：
 *   - 两步流：POST /chat 投递任务（返回 message_id）→ GET /chat/{id}/stream 订阅
 *     SSE 渲染；**执行不再绑定 HTTP 连接** —— 关掉页面任务照跑（F4），
 *     重新打开时从会话历史读到完整答案；
 *   - approval 事件 → 审批卡片（工具/参数/风险/理由 + 批准/拒绝）→
 *     POST /approvals/{id}/decide → 自动重连该消息的 stream 看续跑结果；
 *   - 停止生成 → POST /chat/{id}/cancel（消息标 cancelled，保留已生成内容）；
 *   - 重新生成 → POST /chat/{id}/regenerate（旧消息保留并 superseded_by）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { apiFetch, readableError } from '../lib/api';
import { clearTokens, getEmail, isLoggedIn } from '../lib/auth';

type Role = 'user' | 'assistant' | 'error';

interface Citation {
  n: number;
  title: string;
  page: number | null;
  snippet: string;
  score: number;
}

interface ApprovalCard {
  approval_id: string;
  tool: string;
  args: Record<string, unknown>;
  risk_level: number;
  reason: string;
  decided?: 'approved' | 'rejected';
  busy?: boolean;
}

interface Message {
  id: string;
  role: Role;
  content: string;
  citations?: Citation[];
  degraded?: string[];
  approvals?: ApprovalCard[];
  status?: string;
  /** 服务端 message_id：订阅/取消/重新生成都用它（本地气泡另有渲染 key） */
  serverId?: string;
  streaming?: boolean;
  cancelled?: boolean;
}

interface SseEvent {
  event: string;
  data: Record<string, unknown>;
  lastEventId?: string;
}

/** 解析一个 SSE 事件块（含可选的 id: 行 —— Last-Event-ID 续读用）。 */
function parseSseBlock(block: string): SseEvent | null {
  let event = 'message';
  let id: string | undefined;
  const dataLines: string[] = [];

  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '');
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim();
    } else if (line.startsWith('id:')) {
      id = line.slice('id:'.length).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).replace(/^ /, ''));
    }
  }

  if (dataLines.length === 0) return null;
  try {
    return {
      event,
      data: JSON.parse(dataLines.join('\n')) as Record<string, unknown>,
      lastEventId: id,
    };
  } catch {
    return null;
  }
}

let seq = 0;
const nextId = () => `m${++seq}`;

export default function ChatPage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState<string | null>(null);

  const conversationRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const streamRef = useRef<HTMLDivElement | null>(null);
  /** 当前流对应的服务端消息 id（取消/重连用） */
  const activeMessageRef = useRef<string | null>(null);
  /** 已消费的最后一个事件 id —— 审批续跑/断线重连时用它续读（§7.7） */
  const lastEventIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!isLoggedIn()) {
      router.replace('/login');
      return;
    }
    setEmail(getEmail());
    setReady(true);
    void loadLatestConversation();
  }, [router]);

  /** F4：页面重开时加载最近会话的完整历史（答案在服务端，不依赖浏览器是否在线）。 */
  async function loadLatestConversation() {
    try {
      const res = await apiFetch('/api/v1/conversations?limit=1');
      if (!res.ok) return;
      const list = (await res.json())?.data?.items ?? [];
      if (list.length === 0) return;
      const cid: string = list[0].id;
      conversationRef.current = cid;
      const mres = await apiFetch(`/api/v1/conversations/${cid}/messages?limit=100`);
      if (!mres.ok) return;
      const items = (await mres.json())?.data?.items ?? [];
      const restored: Message[] = [];
      for (const it of items) {
        if (it.role !== 'user' && it.role !== 'assistant') continue;
        restored.push({
          id: nextId(),
          role: it.role as Role,
          content: it.content ?? '',
          citations: it.citations ?? undefined,
          status: it.status,
          serverId: it.id,
        });
      }
      setMessages(restored);
    } catch {
      /* 历史加载失败不阻塞新对话 */
    }
  }

  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  function logout() {
    clearTokens();
    router.push('/login');
  }

  // ---------------- 消息渲染工具（沿用 M3 语义） ----------------

  function appendToken(delta: string) {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== 'assistant') return prev;
      const next = [...prev];
      next[next.length - 1] = { ...last, content: last.content + delta };
      return next;
    });
  }

  function attachCitations(citations: Citation[]) {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== 'assistant') return prev;
      const next = [...prev];
      next[next.length - 1] = { ...last, citations };
      return next;
    });
  }

  function markDegraded(flags: string[], message: string) {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      const next = [...prev];
      if (last && last.role === 'assistant') {
        next[next.length - 1] = { ...last, degraded: flags };
      }
      return [...next, { id: nextId(), role: 'error', content: `[降级] ${message}` }];
    });
  }

  function clearCurrentAssistant() {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== 'assistant') return prev;
      const next = [...prev];
      next[next.length - 1] = { ...last, content: '' };
      return next;
    });
  }

  function pushApproval(card: ApprovalCard) {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== 'assistant') return prev;
      const next = [...prev];
      const existing = last.approvals ?? [];
      if (existing.some((a) => a.approval_id === card.approval_id)) return prev;
      next[next.length - 1] = { ...last, approvals: [...existing, card] };
      return next;
    });
  }

  function handleEvent({ event, data }: SseEvent) {
    switch (event) {
      case 'token': {
        const delta = typeof data.delta === 'string' ? data.delta : '';
        if (delta) appendToken(delta);
        break;
      }
      case 'citations': {
        const list = Array.isArray(data.citations) ? (data.citations as Citation[]) : [];
        if (list.length > 0) attachCitations(list);
        break;
      }
      case 'notice': {
        if (data.clear === true) clearCurrentAssistant();
        break;
      }
      case 'degraded': {
        const flags = Array.isArray(data.degraded) ? (data.degraded as string[]) : [];
        markDegraded(flags, String(data.message ?? '本轮结果有降级'));
        break;
      }
      case 'approval': {
        if (typeof data.approval_id === 'string') {
          pushApproval({
            approval_id: data.approval_id,
            tool: String(data.tool ?? ''),
            args: (data.args as Record<string, unknown>) ?? {},
            risk_level: Number(data.risk_level ?? 2),
            reason: String(data.reason ?? ''),
          });
        }
        break;
      }
      case 'error': {
        const code = typeof data.code === 'string' ? data.code : 'UNKNOWN';
        // STREAM_GONE 不算错误展示（流过期，内容以落库为准）
        if (code !== 'STREAM_GONE') {
          pushError(`[${code}] ${String(data.message ?? '未知错误')}`);
        }
        break;
      }
      case 'done': {
        // 审批挂起：done 里也带 approval 载荷（双保险，防 approval 事件丢失）
        const ap = data.approval as Record<string, unknown> | undefined;
        if (ap && typeof ap.approval_id === 'string') {
          pushApproval({
            approval_id: ap.approval_id,
            tool: String(ap.tool ?? ''),
            args: (ap.args as Record<string, unknown>) ?? {},
            risk_level: Number(ap.risk_level ?? 2),
            reason: String(ap.reason ?? ''),
          });
        }
        const cid = data.conversation_id;
        if (typeof cid === 'string' && cid && !conversationRef.current) {
          conversationRef.current = cid;
        }
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (!last || last.role !== 'assistant') return prev;
          const next = [...prev];
          const finish = String(data.finish_reason ?? 'stop');
          next[next.length - 1] = {
            ...last,
            streaming: false,
            cancelled: finish === 'cancelled',
          };
          return next;
        });
        break;
      }
      default:
        break;
    }
  }

  function pushError(text: string) {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      const base =
        last && last.role === 'assistant' && last.content === '' ? prev.slice(0, -1) : prev;
      return [...base, { id: nextId(), role: 'error', content: text }];
    });
  }

  // ---------------- 事件订阅（两步流的第二步） ----------------

  const subscribe = useCallback(async (messageId: string, signal: AbortSignal) => {
    // 续读：同一消息的流是追加的（审批暂停 → 批准后 resume 继续写），
    // 从头发起会立刻撞上上一轮的 done（M5 修）。带 Last-Event-ID 只读新事件。
    const headers: Record<string, string> = {};
    if (lastEventIdRef.current) headers['Last-Event-ID'] = lastEventIdRef.current;
    const res = await apiFetch(`/api/v1/chat/${messageId}/stream`, { signal, headers });
    if (!res.ok || !res.body) {
      pushError(await readableError(res));
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep = buffer.indexOf('\n\n');
      while (sep !== -1) {
        const block = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const parsed = parseSseBlock(block);
        if (parsed) handleEvent(parsed);
        sep = buffer.indexOf('\n\n');
      }
    }
  }, []);

  // ---------------- 发送（投递 + 订阅） ----------------

  async function send() {
    const content = input.trim();
    if (!content || busy) return;

    setInput('');
    setBusy(true);
    setMessages((prev) => [
      ...prev,
      { id: nextId(), role: 'user', content },
      { id: nextId(), role: 'assistant', content: '', streaming: true },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const res = await apiFetch('/api/v1/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': crypto.randomUUID(),
        },
        body: JSON.stringify({ content, conversation_id: conversationRef.current }),
        signal: controller.signal,
      });

      if (!res.ok) {
        if (res.status !== 401) pushError(await readableError(res));
        return;
      }

      const contentType = res.headers.get('content-type') ?? '';
      if (contentType.startsWith('text/event-stream')) {
        // 幂等重放：响应本身就是 SSE
        const text = await res.text();
        for (const block of text.split('\n\n')) {
          const parsed = parseSseBlock(block);
          if (parsed) handleEvent(parsed);
        }
        return;
      }

      const body = await res.json();
      const mid: string = body?.data?.message_id;
      const cid: string | undefined = body?.data?.conversation_id;
      if (cid && !conversationRef.current) conversationRef.current = cid;
      if (!mid) {
        pushError('服务端未返回 message_id');
        return;
      }
      activeMessageRef.current = mid;
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.role === 'assistant') next[next.length - 1] = { ...last, serverId: mid };
        return next;
      });
      await subscribe(mid, controller.signal);
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        // 用户点「停止」：后台任务由 cancel API 终止，这里不报错
      } else if (err instanceof TypeError) {
        pushError('[BACKEND_OFFLINE] 无法连接服务，请确认容器是否在运行。');
      } else {
        pushError(`请求失败：${(err as Error).message}`);
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
    }
  }

  // ---------------- 停止 / 重新生成 / 审批 ----------------

  async function stopGeneration() {
    const mid = activeMessageRef.current;
    abortRef.current?.abort();
    setBusy(false);
    if (!mid) return;
    try {
      await apiFetch(`/api/v1/chat/${mid}/cancel`, { method: 'POST' });
    } catch {
      /* 取消失败不阻塞 UI */
    }
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last?.role === 'assistant') {
        next[next.length - 1] = { ...last, streaming: false, cancelled: true };
      }
      return next;
    });
  }

  async function regenerate(message: Message) {
    if (!message.serverId || busy) return;
    setBusy(true);
    try {
      const res = await apiFetch(`/api/v1/chat/${message.serverId}/regenerate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        pushError(await readableError(res));
        return;
      }
      const body = await res.json();
      const mid: string = body?.data?.message_id;
      activeMessageRef.current = mid;
      setMessages((prev) => [
        ...prev,
        { id: nextId(), role: 'assistant', content: '', streaming: true, serverId: mid },
      ]);
      const controller = new AbortController();
      abortRef.current = controller;
      await subscribe(mid, controller.signal);
    } finally {
      abortRef.current = null;
      setBusy(false);
    }
  }

  async function decideApproval(card: ApprovalCard, approved: boolean) {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last?.approvals) {
        next[next.length - 1] = {
          ...last,
          approvals: last.approvals.map((a) =>
            a.approval_id === card.approval_id ? { ...a, busy: true } : a,
          ),
        };
      }
      return next;
    });
    try {
      const res = await apiFetch(`/api/v1/approvals/${card.approval_id}/decide`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved, note: approved ? '' : '用户拒绝' }),
      });
      if (!res.ok) {
        pushError(await readableError(res));
        return;
      }
      const body = await res.json();
      const mid: string = body?.data?.message_id;
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.approvals) {
          next[next.length - 1] = {
            ...last,
            approvals: last.approvals.map((a) =>
              a.approval_id === card.approval_id
                ? { ...a, busy: false, decided: approved ? 'approved' : 'rejected' }
                : a,
            ),
          };
        }
        return next;
      });
      // 续跑：订阅同一消息的 stream 看执行/替代回答
      if (mid) {
        activeMessageRef.current = mid;
        setBusy(true);
        const controller = new AbortController();
        abortRef.current = controller;
        await subscribe(mid, controller.signal);
        setBusy(false);
      }
    } finally {
      abortRef.current = null;
    }
  }

  if (!ready) return null;

  return (
    <div className="layout">
      <header className="header">
        <div className="header-row">
          <div>
            <h1>Personal Agent OS</h1>
            <p>M5 · HITL 审批 + 流式健壮性（关页面不丢任务 / 危险动作需确认）</p>
          </div>
          <div className="header-user">
            <span>{email ?? '已登录'}</span>
            <Link className="linklike" href="/kb">知识库</Link>
            <button className="linklike" onClick={logout}>退出</button>
          </div>
        </div>
      </header>

      <div className="stream" ref={streamRef}>
        {messages.length === 0 ? (
          <div className="empty">
            试试：<b>给我讲讲监督学习</b>，或 <b>给 partner@corp.com 发邮件约会议</b>（会弹审批）。
            <br />
            文字应当是<b>逐个出现</b>的 —— 若一次性全出现，说明流被中间层缓冲了。
          </div>
        ) : (
          messages.map((m, i) => {
            const streaming = busy && i === messages.length - 1 && m.role === 'assistant';
            const hasCitations = m.role === 'assistant' && (m.citations?.length ?? 0) > 0;
            const hasDegraded = m.role === 'assistant' && (m.degraded?.length ?? 0) > 0;
            return (
              <div key={m.id} className={`bubble-wrap ${m.role}`}>
                <div className={`bubble ${m.role}`}>
                  {m.content || (m.cancelled ? '（已停止生成）' : '')}
                  {streaming && <span className="caret">▍</span>}
                </div>
                {m.cancelled && <div className="cancelled-badge">已停止生成（后台任务已取消）</div>}
                {hasCitations && (
                  <div className="citations">
                    {m.citations!.map((c) => (
                      <details className="citation" key={c.n}>
                        <summary>
                          <span className="citation-n">[{c.n}]</span>
                          <span className="citation-title">{c.title}</span>
                          {c.page !== null && <span className="citation-page">第 {c.page} 段</span>}
                          <span className="citation-score" title="cosine 相似度">
                            {c.score.toFixed(3)}
                          </span>
                        </summary>
                        <div className="citation-body">{c.snippet}</div>
                      </details>
                    ))}
                  </div>
                )}
                {(m.approvals?.length ?? 0) > 0 && (
                  <div className="approval-cards">
                    {m.approvals!.map((a) => (
                      <div className="approval-card" key={a.approval_id}>
                        <div className="approval-head">
                          <span className="approval-risk">L{a.risk_level} 需审批</span>
                          <span className="approval-tool">{a.tool}</span>
                        </div>
                        <div className="approval-reason">{a.reason}</div>
                        <pre className="approval-args">{JSON.stringify(a.args, null, 2)}</pre>
                        {a.decided ? (
                          <div className={`approval-result ${a.decided}`}>
                            {a.decided === 'approved' ? '已批准，正在执行…' : '已拒绝'}
                          </div>
                        ) : (
                          <div className="approval-actions">
                            <button
                              className="approve"
                              disabled={a.busy}
                              onClick={() => void decideApproval(a, true)}
                            >
                              批准执行
                            </button>
                            <button
                              className="reject"
                              disabled={a.busy}
                              onClick={() => void decideApproval(a, false)}
                            >
                              拒绝
                            </button>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
                {hasDegraded && (
                  <div className="degraded-badge" title={m.degraded!.join(', ')}>
                    ⚠ 部分结论缺少资料支撑（降级：{m.degraded!.join('、')}）
                  </div>
                )}
                {m.role === 'assistant' && !streaming && m.serverId && (
                  <button className="linklike regen" onClick={() => void regenerate(m)}>
                    重新生成
                  </button>
                )}
              </div>
            );
          })
        )}
      </div>

      <div className="composer">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          rows={1}
        />
        {busy ? (
          <button className="stop" onClick={() => void stopGeneration()}>
            停止
          </button>
        ) : (
          <button onClick={() => void send()} disabled={!input.trim()}>
            发送
          </button>
        )}
      </div>
    </div>
  );
}
