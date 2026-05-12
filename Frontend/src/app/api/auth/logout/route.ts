import { NextResponse } from 'next/server';
import { clearSessionCookies } from '../_lib';

export async function POST() {
  await clearSessionCookies();
  return NextResponse.json({ ok: true });
}
