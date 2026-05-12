import type { ChartPayload } from '@/components/Chatscreen/QueryResultChart';
import type { ResultTablePayload } from '@/components/Chatscreen/ResultTablePanel';

export type { ChartPayload, ResultTablePayload };

/** One semicolon-separated sub-query result (workbook / SQLite multipart). */
export interface SubQueryResult {
  index: number;
  question: string;
  response: string;
  sql?: string;
  row_count?: number;
  error?: string;
  chart?: ChartPayload;
  result_table?: ResultTablePayload;
}

interface QueryResponse {
  success: boolean;
  response: string;
  answer_text?: string;
  data_table?: Record<string, unknown>[] | null;
  chart_recommendation?: {
    show_chart: boolean;
    chart_type: 'bar' | 'line' | 'pie' | 'scatter' | 'none';
    x_axis: string;
    y_axis: string;
    title: string;
    rationale: string;
  };
  clarification_needed?: string | null;
  error?: string;
  /** Wall-clock ms for the Python pipeline (FastAPI); also sent as X-Process-Time-Ms */
  duration_ms?: number;
  /** True when the backend served this from Redis QA cache (same question, non-time-volatile). */
  cache_hit?: boolean;
  row_count?: number;
  sql?: string;
  chart?: ChartPayload;
  result_table?: ResultTablePayload;
  result_table_multipart_last_part?: boolean;
  /** Multiple independent answers (semicolon-separated NL in one request). */
  sub_results?: SubQueryResult[];
  followup_questions?: string[];
}

/** `queryChat` return value — check `ok` before treating as a successful data answer. */
export interface QueryChatResult {
  ok: boolean;
  response: string;
  answer_text?: string;
  data_table?: Record<string, unknown>[] | null;
  chart_recommendation?: QueryResponse['chart_recommendation'];
  clarification_needed?: string | null;
  duration_ms?: number;
  cache_hit?: boolean;
  row_count?: number;
  sql?: string;
  chart?: ChartPayload;
  result_table?: ResultTablePayload;
  result_table_multipart_last_part?: boolean;
  sub_results?: SubQueryResult[];
  followup_questions?: string[];
}

/** Completed user → assistant pairs (oldest first). Sent when the server has no in-RAM buffer so multi-turn still works across workers / reloads. */
export type PriorTurn = { user: string; assistant: string };

/** Build up to `maxPairs` completed exchanges from chat messages (oldest first). */
export function buildPriorTurnsForQuery(
  messages: { role: string; content: string }[],
  maxPairs = 6,
): PriorTurn[] {
  const turns: PriorTurn[] = [];
  for (let i = 0; i < messages.length; i++) {
    if (messages[i].role !== 'user') continue;
    const user = messages[i].content.trim();
    if (!user) continue;
    for (let j = i + 1; j < messages.length; j++) {
      if (messages[j].role === 'user') break;
      if (messages[j].role === 'assistant') {
        const chunks: string[] = [];
        let k = j;
        while (k < messages.length && messages[k].role === 'assistant') {
          chunks.push(messages[k].content.trim());
          k += 1;
        }
        turns.push({ user, assistant: chunks.join('\n\n').slice(0, 16000) });
        break;
      }
    }
  }
  return turns.slice(-maxPairs);
}

interface QueryParams {
  question: string;
  sessionId: string;
  /** Last user question that produced `previousSql` — sent so follow-ups work if server buffer is empty. */
  previousQuestion?: string;
  /** SQL from the last successful assistant answer — anchors short follow-ups like "by territory". */
  previousSql?: string;
  /** Recent Q→A pairs so the API can seed context when its buffer is empty (multi-turn / multi-worker). */
  priorTurns?: PriorTurn[];
  /** When true, bypasses both local and Redis cache — always runs a fresh LLM call. */
  forceRefresh?: boolean;
}

/**
 * Where to POST /query (JSON body — no query string; avoids URL length limits on previous_sql):
 * - In production → always use same-origin **POST /api/query** so Next.js server proxies to
 *   SDA_BACKEND_URL and browser CORS/network differences don't break requests.
 * - In development, if NEXT_PUBLIC_SERVER_URL is set → browser calls FastAPI **POST /query**.
 *   Use the API origin only (e.g. http://127.0.0.1:8000), not .../api.
 * - Else in development → same-origin **POST /api/query**.
 */
function buildQueryUrl(): string {
  if (process.env.NODE_ENV === 'production') {
    return '/api/query';
  }

  const direct = process.env.NEXT_PUBLIC_SERVER_URL?.trim().replace(/\/$/, '');
  if (direct) {
    const base = direct.replace(/\/api\/?$/, '');
    return new URL('/query', base.endsWith('/') ? base : `${base}/`).toString();
  }
  return '/api/query';
}

function parseChartPayload(
  chart: unknown,
): ChartPayload | undefined {
  if (
    chart &&
    typeof chart === 'object' &&
    (chart as { kind?: string }).kind &&
    ['bar', 'pie', 'line', 'scatter'].includes(String((chart as { kind: string }).kind)) &&
    Array.isArray((chart as { data?: unknown }).data)
  ) {
    const c = chart as ChartPayload & {
      lineSeriesKeys?: string[];
      title?: string;
      description?: string;
      showGrowthLines?: boolean;
    };
    return {
      kind: c.kind,
      data: c.data as Record<string, unknown>[],
      ...(Array.isArray(c.lineSeriesKeys) && c.lineSeriesKeys.length > 0
        ? { lineSeriesKeys: c.lineSeriesKeys }
        : {}),
      ...(typeof c.title === 'string' && c.title.trim() ? { title: c.title.trim() } : {}),
      ...(typeof c.description === 'string' && c.description.trim()
        ? { description: c.description.trim() }
        : {}),
      ...(c.showGrowthLines === true ? { showGrowthLines: true } : {}),
    };
  }
  return undefined;
}

function parseResultTablePayload(rt: unknown): ResultTablePayload | undefined {
  if (
    rt &&
    typeof rt === 'object' &&
    Array.isArray((rt as { columns?: unknown }).columns) &&
    Array.isArray((rt as { rows?: unknown }).rows) &&
    (rt as { columns: string[] }).columns.length > 0 &&
    (rt as { rows: unknown[] }).rows.length > 0
  ) {
    const t = rt as ResultTablePayload & {
      total_row_count?: number;
      truncated?: boolean;
      truncated_for_storage?: boolean;
      suppress_remaining_rows?: boolean;
    };
    return {
      columns: t.columns,
      rows: t.rows as Record<string, unknown>[],
      total_row_count: typeof t.total_row_count === 'number' ? t.total_row_count : undefined,
      truncated: Boolean(t.truncated),
      truncated_for_storage: Boolean(t.truncated_for_storage),
      suppress_remaining_rows: Boolean(t.suppress_remaining_rows),
    };
  }
  return undefined;
}

/** Mirrors ``Backend/src/arcutis_public_replies.py`` — keep wording in sync. */
export const PHARMA_ASSISTANT_PUBLIC_REPLY =
  "I'm the Arcutis Data Assistant. I can only help with Arcutis and pharmaceutical-related " +
  "queries — things like HCP data, prescribing trends, territory performance, ZORYVE TRx, " +
  "payer mix, and market insights. Could you share what you'd like to explore?";

export const OFFTOPIC_DENY_REPLY =
  "That topic is outside my scope. I'm the Arcutis Data Assistant, focused exclusively on " +
  "Arcutis products, HCP analytics, prescribing data, and pharmaceutical competitor insights. " +
  "I'm not able to help with that request.";

export const GIBBERISH_REPLY =
  "I'm the Arcutis Data Assistant. I can only help with Arcutis and pharmaceutical-related queries.";

export const PHARMA_ASSISTANT_FALLBACK_REPLY =
  "I'm the Arcutis Data Assistant. I can only help with Arcutis and pharmaceutical-related queries.";

export const PHARMA_TIMEOUT_REPLY =
  "That's taking longer than expected — the database or AI pipeline may be under load. " +
  "Please try again in a moment. If the issue continues, try rephrasing your question or " +
  "narrowing the scope (e.g. a specific region or time period).";

export const PHARMA_PARSE_ERROR_REPLY =
  "I wasn't able to read the response correctly. Could you try rephrasing your question? " +
  "I'm here to help with HCP data, ZORYVE TRx, territory performance, and related insights.";

const CANNED_FAILURE_REPLIES: readonly string[] = [
  PHARMA_ASSISTANT_PUBLIC_REPLY,
  OFFTOPIC_DENY_REPLY,
  GIBBERISH_REPLY,
];

/** When a multipart sub-query returns an error string, map known canned replies only. */
export function publicReplyForSubQueryError(error: string | undefined): string {
  const t = (error || '').trim();
  if (CANNED_FAILURE_REPLIES.includes(t)) return t;
  return PHARMA_ASSISTANT_PUBLIC_REPLY;
}

export const queryChat = async ({
  question,
  sessionId,
  previousQuestion,
  previousSql,
  priorTurns,
  forceRefresh = false,
}: QueryParams): Promise<QueryChatResult> => {
  const url = buildQueryUrl();
  const body = JSON.stringify({
    question,
    session_id: sessionId,
    ...(previousQuestion?.trim() && previousSql?.trim()
      ? { previous_question: previousQuestion.trim(), previous_sql: previousSql.trim() }
      : {}),
    ...(priorTurns && priorTurns.length > 0 ? { prior_turns: priorTurns } : {}),
    ...(forceRefresh ? { force_refresh: true } : {}),
  });

  /** NL→SQL pipelines can exceed 60s; match proxy default (10 min unless overridden). */
  const _ft = Number(process.env.NEXT_PUBLIC_QUERY_FETCH_TIMEOUT_MS);
  const fetchTimeoutMs = Math.max(
    120_000,
    Number.isFinite(_ft) && _ft > 0 ? _ft : 600_000,
  );

  const doFetch = () =>
    fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body,
      signal: AbortSignal.timeout(fetchTimeoutMs),
    });

  let response: Response;
  try {
    response = await doFetch();
    // Render free instances can briefly return gateway errors right after wake-up.
    // Retry once to smooth transient 502/503 without user-visible failure.
    if (response.status === 502 || response.status === 503) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      response = await doFetch();
    }
  } catch (e) {
    const aborted =
      e instanceof Error &&
      (e.name === 'AbortError' ||
        (typeof DOMException !== 'undefined' && e instanceof DOMException && e.name === 'AbortError'));
    if (aborted) {
      return { ok: false, response: PHARMA_TIMEOUT_REPLY };
    }
    return { ok: false, response: PHARMA_ASSISTANT_FALLBACK_REPLY };
  }

  const bodyText = await response.text();

  if (!response.ok) {
    return { ok: false, response: PHARMA_ASSISTANT_FALLBACK_REPLY };
  }

  let data: QueryResponse;
  try {
    data = JSON.parse(bodyText) as QueryResponse;
  } catch {
    return { ok: false, response: PHARMA_PARSE_ERROR_REPLY };
  }

  if (typeof data.duration_ms === 'number' && process.env.NODE_ENV === 'development') {
    console.info(`[SDA] query duration: ${data.duration_ms}ms`);
  }

  if (data.success) {
    const chartPayload = parseChartPayload(data.chart);
    const resultTable = parseResultTablePayload(data.result_table);
    const rawSubs = data.sub_results;
    const sub_results: SubQueryResult[] | undefined =
      Array.isArray(rawSubs) && rawSubs.length > 0
        ? rawSubs.map((sr) => {
            const o = sr as unknown as Record<string, unknown>;
            return {
              index: typeof o.index === 'number' ? o.index : Number(o.index) || 0,
              question: typeof o.question === 'string' ? o.question : '',
              response: typeof o.response === 'string' ? o.response : '',
              sql: typeof o.sql === 'string' ? o.sql : undefined,
              row_count: typeof o.row_count === 'number' ? o.row_count : undefined,
              error: typeof o.error === 'string' ? o.error : undefined,
              chart: parseChartPayload(o.chart),
              result_table: parseResultTablePayload(o.result_table),
            };
          })
        : undefined;
    return {
      ok: true,
      response: data.answer_text || data.response,
      answer_text: data.answer_text || data.response,
      data_table: Array.isArray(data.data_table) ? data.data_table : null,
      chart_recommendation: data.chart_recommendation,
      clarification_needed: typeof data.clarification_needed === 'string' ? data.clarification_needed : null,
      duration_ms: data.duration_ms,
      cache_hit: Boolean(data.cache_hit),
      row_count: typeof data.row_count === 'number' ? data.row_count : undefined,
      sql: typeof data.sql === 'string' ? data.sql : undefined,
      chart: chartPayload,
      result_table: resultTable,
      result_table_multipart_last_part: Boolean(data.result_table_multipart_last_part),
      sub_results,
      followup_questions: Array.isArray(data.followup_questions) ? data.followup_questions as string[] : [],
    };
  }
  return {
    ok: false,
    // Prefer clarification from backend when present (api_server.py sets this on some paths)
    response:
      typeof data.clarification_needed === 'string' && data.clarification_needed.trim()
        ? data.clarification_needed.trim()
        : typeof data.response === 'string' && data.response.trim()
          ? data.response.trim()
          : PHARMA_ASSISTANT_FALLBACK_REPLY,
    clarification_needed:
      typeof data.clarification_needed === 'string' ? data.clarification_needed : null,
    duration_ms: data.duration_ms,
    sql: typeof data.sql === 'string' ? data.sql : undefined,
  };
};
const CHAT_SESSION_MAP_KEY = 'chatSessionByChatId';

/** One backend `session_id` per UI chat so switching threads keeps separate context. */
export function getOrCreateSessionIdForChat(chatId: string): string {
  if (typeof window === 'undefined') return '1234';
  const raw = localStorage.getItem(CHAT_SESSION_MAP_KEY);
  const map: Record<string, string> = raw ? (JSON.parse(raw) as Record<string, string>) : {};
  if (!map[chatId]) {
    map[chatId] = `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
    localStorage.setItem(CHAT_SESSION_MAP_KEY, JSON.stringify(map));
  }
  return map[chatId];
}
