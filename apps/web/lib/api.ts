/**
 * 统一 API 客户端：自动附带 Bearer 头 + 401 时单飞（single-flight）刷新重试。
 *
 * 刷新流程（§12.2「token 拦截刷新」）：
 *   请求 → 401 → 用 refresh_token 调 /auth/refresh → 成功则换新对并重放原请求；
 *   刷新失败 → 清空本地凭据 → 跳转 /login。
 * 并发的多个 401 只触发一次刷新（共享同一个 promise），避免刷新风暴。
 */

import { clearTokens, getAccessToken, getRefreshToken, setTokens } from './auth';

let refreshInFlight: Promise<boolean> | null = null;

async function doRefresh(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;

  try {
    const res = await fetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) return false;
    const body = await res.json();
    const data = body?.data;
    if (!data?.access_token || !data?.refresh_token) return false;
    setTokens(data.access_token, data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

export async function apiFetch(
  input: string,
  init: RequestInit = {},
  retryOn401 = true,
): Promise<Response> {
  const headers = new Headers(init.headers);
  const access = getAccessToken();
  if (access) headers.set('Authorization', `Bearer ${access}`);

  const res = await fetch(input, { ...init, headers });

  if (res.status === 401 && retryOn401) {
    // 单飞：并发 401 只做一次刷新
    refreshInFlight ||= doRefresh().finally(() => {
      refreshInFlight = null;
    });
    const refreshed = await refreshInFlight;

    if (refreshed) {
      return apiFetch(input, init, false);
    }
    clearTokens();
    if (typeof window !== 'undefined') {
      window.location.href = '/login';
    }
  }
  return res;
}

/** 解析统一错误体 {error:{code,message}}，给出可读文案。 */
export async function readableError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    const err = body?.error;
    if (err?.code === 'CONFLICT') {
      return '该会话正在处理中，请稍候再试。';
    }
    return err?.message ? `${err.code}：${err.message}` : `请求失败（HTTP ${res.status}）`;
  } catch {
    return `请求失败（HTTP ${res.status}）`;
  }
}
