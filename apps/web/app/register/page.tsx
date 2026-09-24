'use client';

/**
 * 注册页（M1-9）。成功后引导去登录（B3 验收流程：注册 → 登录）。
 */

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (password.length < 8) {
      setError('密码至少 8 位。');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email,
          password,
          display_name: displayName || null,
        }),
      });
      if (!res.ok) {
        let msg = `注册失败（HTTP ${res.status}）`;
        try {
          const body = await res.json();
          if (body?.error?.message) msg = body.error.message;
        } catch { /* 非 JSON 响应，保留默认文案 */ }
        setError(msg);
        return;
      }
      // 注册成功 → 去登录（保持与验收流程一致：注册 → 登录）
      router.push('/login');
    } catch {
      setError('网络异常，请确认服务是否在运行。');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <h1>注册 · Personal Agent OS</h1>
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
          昵称（可选）
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="怎么称呼你"
            maxLength={64}
          />
        </label>
        <label>
          密码（至少 8 位）
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            required
            minLength={8}
            autoComplete="new-password"
          />
        </label>
        {error && <p className="auth-error">{error}</p>}
        <button type="submit" disabled={busy || !email || !password}>
          {busy ? '注册中…' : '注册'}
        </button>
        <p className="auth-alt">
          已有账号？<Link href="/login">登录</Link>
        </p>
      </form>
    </main>
  );
}
