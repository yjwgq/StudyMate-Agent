'use client';

/**
 * M0 聊天页 —— 端到端最小闭环的前端一侧。
 *
 * 这个页面的唯一使命：**证明 SSE 流没有被任何中间层缓冲**。
 * 所以它刻意用原生 `fetch` + `ReadableStream` 手动解析 SSE，
 * 而不是用封装好的库 —— 出问题时能直接定位到是哪一层没吐数据。
 *
 * 后续演进（设计文档 v1.1 §12）：
 *   M5 → 换用 Vercel AI SDK 的自定义 transport，接入 tool_start / approval 等事件
 *   M6 → 加 KaTeX 公式渲染与降级角标
 */

import { useEffect, useRef, useState } from 'react';

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

/**
 * 解析一个 SSE 事件块（不含结尾空行）。
 *
 * SSE 的块格式：
 *     event: token
 *     data: {"delta":"你"}
 *
 * 注意 data 可能跨多行，按规范要用 \n 拼回再解析。
 */
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
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const streamRef = useRef<HTMLDivElement | null>(null);

  // 有新内容时自动滚到底部
  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // 组件卸载时中断在途请求，避免内存泄漏
  useEffect(() => () => abortRef.current?.abort(), []);

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
      // 出错或被中断时，末尾通常留着一个还没吐字的 assistant 占位气泡，
      // 先把它摘掉再追加错误气泡，否则界面上会挂一个空白气泡。
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
        // 后端把异常转成 error 事件而不是切断连接，
        // 所以这里能拿到可读的错误信息 —— 这是验收项 A4 的核心。
        // code 来自后端错误码字典（LLM_OFFLINE / INTERNAL / ...），必须透传，
        // 验收项 A4 检查的正是页面上能看到 LLM_OFFLINE 这个标识。
        const code = typeof data.code === 'string' ? data.code : 'UNKNOWN';
        pushError(`[${code}] ${String(data.message ?? '未知错误')}`);
        break;
      }
      case 'done':
        break;
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
      const res = await fetch('/api/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
        signal: controller.signal,
      });

      // 后端若在「建流之前」失败（如未配置 Key），返回的是普通 JSON 而非事件流
      if (!res.ok) {
        const detail = await res.text().catch(() => '');
        // 502/503/504：Caddy 还活着但 upstream(api) 已停 —— 验收项 A5 的场景，
        // 页面必须显示 BACKEND_OFFLINE 而不是一句裸的 HTTP 502
        if (res.status === 502 || res.status === 503 || res.status === 504) {
          throw new Error(
            `[BACKEND_OFFLINE] 后端不可达（HTTP ${res.status}）${detail ? ` — ${detail}` : ''}`,
          );
        }
        throw new Error(`后端返回 HTTP ${res.status}${detail ? ` — ${detail}` : ''}`);
      }
      if (!res.body) {
        throw new Error('响应没有 body，无法读取流');
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
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        pushError('已中断本次生成。');
      } else if (err instanceof TypeError) {
        // fetch 抛 TypeError = 连 Caddy 的 TCP 都没建立（整个 compose 都停了），
        // 同样归入 BACKEND_OFFLINE，与 A5 的语义保持一致
        pushError('[BACKEND_OFFLINE] 无法连接服务，请确认容器是否在运行。');
      } else {
        pushError(`请求失败：${(err as Error).message}`);
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
    }
  }

  return (
    <div className="layout">
      <header className="header">
        <h1>Personal Agent OS</h1>
        <p>M0 · 端到端最小闭环（浏览器 → FastAPI → DeepSeek → SSE）</p>
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
