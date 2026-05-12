import crypto from 'crypto';
import { cookies } from 'next/headers';

const SESSION_COOKIE = 'sda_session';
const USER_COOKIE = 'sda_user';
const SESSION_TTL_SECONDS = 60 * 60 * 8;

function authSecret(): string {
  return process.env.SDA_AUTH_SECRET || process.env.NEXTAUTH_SECRET || 'dev-only-secret-change-me';
}

function configuredUser(): string {
  return (process.env.SDA_AUTH_USERNAME || 'admin').trim();
}

function configuredPassword(): string {
  return process.env.SDA_AUTH_PASSWORD || 'admin123';
}

function sign(username: string): string {
  return crypto.createHmac('sha256', authSecret()).update(username).digest('hex');
}

export function validateCredentials(username: string, password: string): boolean {
  return username === configuredUser() && password === configuredPassword();
}

export async function setSessionCookies(username: string): Promise<void> {
  const store = await cookies();
  store.set(SESSION_COOKIE, sign(username), {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    path: '/',
  });
  store.set(USER_COOKIE, username, {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    path: '/',
  });
}

export async function clearSessionCookies(): Promise<void> {
  const store = await cookies();
  store.delete(SESSION_COOKIE);
  store.delete(USER_COOKIE);
}

export async function readSession(): Promise<{ authenticated: boolean; username?: string }> {
  const store = await cookies();
  const token = store.get(SESSION_COOKIE)?.value;
  const username = store.get(USER_COOKIE)?.value;
  if (!token || !username) {
    return { authenticated: false };
  }
  if (token !== sign(username)) {
    return { authenticated: false };
  }
  return { authenticated: true, username };
}
