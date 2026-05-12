import { NextRequest, NextResponse } from 'next/server';
import { setSessionCookies, validateCredentials } from '../_lib';

export async function POST(req: NextRequest) {
  try {
    const body = (await req.json()) as { username?: string; password?: string };
    const username = (body.username || '').trim();
    const password = body.password || '';
    if (!username || !password) {
      return NextResponse.json({ ok: false, error: 'Missing username or password.' }, { status: 400 });
    }
    if (!validateCredentials(username, password)) {
      return NextResponse.json({ ok: false, error: 'Invalid username or password.' }, { status: 401 });
    }
    await setSessionCookies(username);
    return NextResponse.json({ ok: true, username });
  } catch {
    return NextResponse.json({ ok: false, error: 'Invalid login request.' }, { status: 400 });
  }
}
