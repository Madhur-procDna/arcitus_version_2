import { NextResponse } from 'next/server';
import { readSession } from '../_lib';

export async function GET() {
  const session = await readSession();
  // Always return 200 — authentication state is in the body.
  // Returning 401 here causes spurious error logs on every page load
  // while cookies are still propagating (especially on first paint / remount).
  return NextResponse.json(session, {
    status: 200,
    headers: { 'Cache-Control': 'no-store' },
  });
}
