'use client';

/**
 * M1 聊天页 —— 在 M0 端到端闭环之上接入认证与会话语义：
 *   - 未登录 → 跳转 /login；401 → apiFetch 内部刷新重试，刷新失败跳登录；
 *   - 请求带 Idempotency-Key（每次用户输入生成 UUID，§7.6）；
 *   - 首条消息不带 conversation_id → 服务端建会话；
 *     done 事件返回 conversation_id 后，本页后续消息都挂在同一会话上；
 *   - 409（会话锁 / 幂等进行中）展示可读提示（B6 的前端表现）。
 *
 * 仍然手写 SSE 解析：出问题时能直接定位是哪一层没吐数据。
 */

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { apiFetch, readableError } from '../lib/api';
import { clearTokens, getEmail, isLoggedIn } from '../lib/auth';

type Role = 'user' | 'assistant' | 'error';

interface Message {
  id: string;
  role: Role;
  content: string;
}

interface SseEvent {
  event: string;
  data: Record<string, unknown>;
}

/** 解析一个 SSE 事件块（不含结尾空行）。 */
function parseSseBlock(block: string): SseEvent | null {
  let event = 'message';
  const dataLines: string[] = [];

  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '');
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      // 规范允许冒号后跟一个空格，需要剥掉
      dataLines.push(line.slice('data:'.length).replace(/^ /, ''));
    }
  }

  if (dataLines.length === 0) return null;

  try {
    return { event, data: JSON.parse(dataLines.join('\n')) as Record<string, unknown> };
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

  // 未登录 → 跳转 /login
  useEffect(() => {
    if (!isLoggedIn()) {
      router.replace('/login');
      return;
    }
    setEmail(getEmail());
    setReady(true);
  }, [router]);

  // 有新内容时自动滚到底部
  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // 组件卸载时中断在途请求，避免内存泄漏
  useEffect(() => () => abortRef.current?.abort(), []);

  function logout() {
    clearTokens();
    router.push('/login');
  }

  function appendToken(delta: string) {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== 'assistant') return prev;
      const next = [...prev];
      next[next.length - 1] = { ...last, content: last.content + delta };
      return next;
    });
  }

  function pushError(text: string) {
    setMessages((prev) => {
      // 出错时，末尾通常留着一个还没吐字的 assistant 占位气泡，先摘掉
      const last = prev[prev.length - 1];
      const base =
        last && last.role === 'assistant' && last.content === '' ? prev.slice(0, -1) : prev;
      return [...base, { id: nextId(), role: 'error', content: text }];
    });
  }

  function handleEvent({ event, data }: SseEvent) {
    switch (event) {
      case 'token': {
        const delta = typeof data.delta === 'string' ? data.delta : '';
        if (delta) appendToken(delta);
        break;
      }
      case 'error': {
        const code = typeof data.code === 'string' ? data.code : 'UNKNOWN';
        pushError(`[${code}] ${String(data.message ?? '未知错误')}`);
        break;
      }
      case 'done': {
        // 服务端建会话后回传 id：本页后续消息挂同一会话
        const cid = data.conversation_id;
        if (typeof cid === 'string' && cid && !conversationRef.current) {
          conversationRef.current = cid;
        }
        break;
      }
      default:
        break;
    }
  }

  async function send() {
    const content = input.trim();
    if (!content || busy) return;

    setInput('');
    setBusy(true);
    setMessages((prev) => [
      ...prev,
      { id: nextId(), role: 'user', content },
      { id: nextId(), role: 'assistant', content: '' },
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
        body: JSON.stringify({
          content,
          conversation_id: conversationRef.current,
        }),
        signal: controller.signal,
      });

      if (!res.ok) {
        // 409（会话锁/幂等）、401（刷新失败已跳登录）、其他 → 统一可读文案
        if (res.status !== 401) {
          pushError(await readableError(res));
        }
        return;
      }
      if (!res.body) {
        pushError('响应没有 body，无法读取流');
        return;
      }

      // 服务端可能新建了会话：响应头直接带出（done 事件也会带，双保险）
      const headerCid = res.headers.get('X-Conversation-Id');
      if (headerCid && !conversationRef.current) conversationRef.current = headerCid;

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
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        pushError('已中断本次生成。');
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

  if (!ready) return null;

  return (
    <div className="layout">
      <header className="header">
        <div className="header-row">
          <div>
            <h1>Personal Agent OS</h1>
            <p>M1 · 多租户认证版（JWT / RLS / 会话锁 / 幂等）</p>
          </div>
          <div className="header-user">
            <span>{email ?? '已登录'}</span>
            <button className="linklike" onClick={logout}>退出</button>
          </div>
        </div>
      </header>

      <div className="stream" ref={streamRef}>
        {messages.length === 0 ? (
          <div className="empty">
            输入一句话试试。
            <br />
            文字应当是<b>逐个出现</b>的 —— 若一次性全出现，说明流被中间层缓冲了。
          </div>
        ) : (
          messages.map((m, i) => {
            const streaming = busy && i === messages.length - 1 && m.role === 'assistant';
            return (
              <div key={m.id} className={`bubble ${m.role}`}>
                {m.content}
                {streaming && <span className="caret">▍</span>}
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
            // isComposing：中文输入法候选阶段按 Enter 不应触发发送
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          rows={1}
        />
        {busy ? (
          <button className="stop" onClick={() => abortRef.current?.abort()}>
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
