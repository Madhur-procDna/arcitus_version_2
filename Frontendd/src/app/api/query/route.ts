import { NextRequest, NextResponse } from 'next/server';
import { readSession } from '../auth/_lib';

/** Allow long NL→SQL→DB→LLM runs (default 10 minutes). Set SDA_QUERY_PROXY_TIMEOUT_MS in .env.local if needed. */
export const maxDuration = 300;

const _proxyMs = Number(process.env.SDA_QUERY_PROXY_TIMEOUT_MS);
const PROXY_FETCH_MS = Math.max(
  120_000,
  Number.isFinite(_proxyMs) && _proxyMs > 0 ? _proxyMs : 600_000,
);

/**
 * Server-side proxy to the Python FastAPI app (avoids browser CORS/preflight to the API port).
 * Set SDA_BACKEND_URL in .env.local (server-only), default http://127.0.0.1:8001.
 * Guard against stale shell-level SDA_BACKEND_URL values on 8002.
 */
function resolveBackendBase(): string {
  const raw = process.env.SDA_BACKEND_URL?.replace(/\/$/, '') || 'http://127.0.0.1:8001';
  try {
    const u = new URL(raw);
    if (u.port === '8001') {
      u.port = '8001';
      return u.origin;
    }
    return u.origin;
  } catch {
    return 'http://127.0.0.1:8001';
  }
}

const BACKEND = resolveBackendBase();

/** On Windows, Node sometimes resolves 127.0.0.1 vs localhost differently — try both. */
function backendBaseCandidates(primary: string): string[] {
  const out: string[] = [primary];
  try {
    const u = new URL(primary);
    if (u.hostname === '127.0.0.1') {
      u.hostname = 'localhost';
      out.push(u.origin);
    } else if (u.hostname === 'localhost') {
      u.hostname = '127.0.0.1';
      out.push(u.origin);
    }
  } catch {
    /* ignore */
  }
  return [...new Set(out)];
}

export async function POST(req: NextRequest) {
  const session = await readSession();
  if (!session.authenticated) {
    return NextResponse.json(
      { success: false, response: '', error: 'Unauthorized. Please log in.' },
      { status: 401 },
    );
  }

  const ct = req.headers.get('content-type') || '';
  let bodyText: string;

  if (ct.includes('application/json')) {
    bodyText = await req.text();
    try {
      const parsed = JSON.parse(bodyText) as {
        question?: string;
        session_id?: string;
        previous_question?: string;
        previous_sql?: string;
      };
      if (!parsed.question?.trim() || !parsed.session_id?.trim()) {
        return NextResponse.json(
          {
            success: false,
            response: '',
            error: 'Missing question or session_id in JSON body.',
          },
          { status: 400 },
        );
      }
    } catch {
      return NextResponse.json(
        { success: false, response: '', error: 'Invalid JSON body.' },
        { status: 400 },
      );
    }
  } else {
    const { searchParams } = new URL(req.url);
    const question = searchParams.get('question');
    const sessionId = searchParams.get('session_id');
    if (!question?.trim() || !sessionId?.trim()) {
      return NextResponse.json(
        {
          success: false,
          response: '',
          error: 'Expected Content-Type: application/json or question & session_id query parameters.',
        },
        { status: 400 },
      );
    }
    bodyText = JSON.stringify({
      question: question.trim(),
      session_id: sessionId.trim(),
      previous_question: searchParams.get('previous_question') || undefined,
      previous_sql: searchParams.get('previous_sql') || undefined,
    });
  }

  let lastErr: unknown;
  for (const base of backendBaseCandidates(BACKEND)) {
    const target = new URL('/query', base);
    try {
      const upstream = await fetch(target.toString(), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(process.env.SDA_BACKEND_API_KEY
            ? { 'X-API-Key': process.env.SDA_BACKEND_API_KEY }
            : {}),
        },
        body: bodyText,
        cache: 'no-store',
        signal: AbortSignal.timeout(PROXY_FETCH_MS),
      });
      const text = await upstream.text();
      const h = new Headers({ 'Content-Type': 'application/json' });
      const pt = upstream.headers.get('x-process-time-ms');
      if (pt) {
        h.set('X-Process-Time-Ms', pt);
      }
      return new NextResponse(text, {
        status: upstream.status,
        headers: h,
      });
    } catch (e) {
      lastErr = e;
    }
  }

  const msg = lastErr instanceof Error ? lastErr.message : String(lastErr);
  const tried = backendBaseCandidates(BACKEND).join(', ');
  const healthUrl = `${BACKEND}/health`;
  return NextResponse.json(
    {
      success: false,
      response: '',
      error: [
        `Python API is not reachable (tried: ${tried}).`,
        `Configured base URL: ${BACKEND} (set SDA_BACKEND_URL in Frontendd/.env.local if you use another port, e.g. 8001 — then restart npm run dev).`,
        'Start the API from Backend\\src: python -m uvicorn api_server:app --host 127.0.0.1 --port <PORT>',
        'Port must match SDA_BACKEND_URL in Frontendd/.env.local (e.g. 8000 or 8001). Or: cd Backend then .\\run_api.ps1',
        `Confirm in browser: ${healthUrl} → {"status":"ok"}`,
        `fetch: ${msg}`,
      ].join('\n'),
    },
    { status: 502 },
  );
}
