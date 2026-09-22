'use client';

/**
 * 登录页（M1-9）。
 *
 * 成功后把 token 对写入 localStorage 并跳回聊天页；
 * 失败展示统一错误体里的可读信息（AUTH_FAILED / VALIDATION）。
 */

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { setTokens } from '../../lib/auth';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        let msg = `登录失败（HTTP ${res.status}）`;
        try {
          const body = await res.json();
          if (body?.error?.message) msg = body.error.message;
        } catch { /* 非 JSON 响应，保留默认文案 */ }
        setError(msg);
        return;
      }
      const body = await res.json();
      const data = body.data;
      setTokens(data.access_token, data.refresh_token, data.user?.email);
      router.push('/');
    } catch {
      setError('网络异常，请确认服务是否在运行。');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <h1>登录 · Personal Agent OS</h1>
        <label>
          邮箱
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            required
            autoComplete="email"
          />
        </label>
        <label>
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            required
            autoComplete="current-password"
          />
        </label>
        {error && <p className="auth-error">{error}</p>}
        <button type="submit" disabled={busy || !email || !password}>
          {busy ? '登录中…' : '登录'}
        </button>
        <p className="auth-alt">
          还没有账号？<Link href="/register">注册</Link>
        </p>
      </form>
    </main>
  );
}
