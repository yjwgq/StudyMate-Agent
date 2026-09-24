/**
 * 认证状态存取（localStorage）。
 *
 * M1 采用「localStorage 存 JWT + Bearer 头」的最小方案；
 * 升级到 httpOnly cookie 属于部署期加固（设计文档 §13），功能不变。
 */

const ACCESS_KEY = 'pg_access_token';
const REFRESH_KEY = 'pg_refresh_token';
const EMAIL_KEY = 'pg_email';

export function getAccessToken(): string | null {
  return typeof window === 'undefined' ? null : localStorage.getItem(ACCESS_KEY);
}

export function getRefreshToken(): string | null {
  return typeof window === 'undefined' ? null : localStorage.getItem(REFRESH_KEY);
}

export function getEmail(): string | null {
  return typeof window === 'undefined' ? null : localStorage.getItem(EMAIL_KEY);
}

export function setTokens(access: string, refresh: string, email?: string): void {
  localStorage.setItem(ACCESS_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
  if (email) localStorage.setItem(EMAIL_KEY, email);
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(EMAIL_KEY);
}

export function isLoggedIn(): boolean {
  return Boolean(getAccessToken() || getRefreshToken());
}
